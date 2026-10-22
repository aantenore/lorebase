"""Erasure of personal data held in the vault (design 13.5, AC-22).

`POST /domains/{d}/vault/erase` erases the vaulted values of one data subject, needing
`pii.erase` on the whole Domain (only `admin` holds it among the built-in roles):

- by Record: `kind` and `key`, optionally limited to some vault `attributes`; every value the
  Record ever held in them, on every branch and Revision;
- by value: `kind`, a vault `attribute` and the `value`; every Record of the Kind holding it,
  on every branch and Revision.

Revisions keep their hashes and structure: Record objects hold tokens, and reads of an erased
token answer `keeper.reads.sensitivity.erased_value`. Erasing again changes nothing (`erased`
counts the values erased by this request). The erasure and its audit entry (action
`vault.erase`, with the Kind, the key or Attribute and the count, never the value) commit in
one transaction. Attribute and Kind names are read in the schema of `branch` (default: the
Domain's default branch).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, JsonValue, model_validator

from lorebase_core.model import Kind, Schema
from lorebase_core.types import EncodingError, encode_value
from lorebase_keeper.auth.audit import annotate, suppress
from lorebase_keeper.auth.deps import Access, require
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.context import StoreDep, require_domain
from lorebase_keeper.errors import (
    CONFLICT,
    NOT_FOUND,
    Problem,
    ProblemError,
    ValidationProblem,
)
from lorebase_keeper.reading.problems import invalid
from lorebase_store.auth import AuditRepository, NewAuditEntry
from lorebase_store.commit.schema import load_compiled
from lorebase_store.repositories import MaterializedRepository
from lorebase_store.vault import erase_matching, erase_record

router = APIRouter(tags=["vault"])

ERASE_ACTION = "vault.erase"

_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks pii.erase on the Domain."},
    404: {"model": Problem, "description": "Domain, branch or Kind not found."},
    409: {"model": Problem, "description": "Erasure by value needs the vault keys."},
    422: {"model": ValidationProblem, "description": "Invalid request."},
}

Erase = Annotated[Access, Depends(require(Permission.PII_ERASE))]


class EraseIn(BaseModel):
    """Whose values to erase: a Record (`key`, optional `attributes`) or a value
    (`attribute` and `value`)."""

    kind: str = Field(description="Kind api name.")
    key: str | None = Field(default=None, description="Key of the Record whose values to erase.")
    attributes: list[str] | None = Field(
        default=None,
        min_length=1,
        description="With `key`: only these vault Attributes (api names); default: all.",
    )
    attribute: str | None = Field(
        default=None, description="Vault Attribute (api name) whose `value` to erase."
    )
    value: JsonValue = Field(
        default=None, description="With `attribute`: the value to erase in every Record."
    )
    branch: str | None = Field(
        default=None, description="Branch whose schema names Kinds and Attributes."
    )

    @model_validator(mode="after")
    def _one_subject(self) -> Self:
        by_key = self.key is not None
        by_value = self.attribute is not None
        if by_key == by_value:
            raise ValueError("give either key (and attributes) or attribute and value")
        if by_value and (self.value is None or self.attributes is not None):
            raise ValueError("erasure by value takes attribute and value only")
        return self


class EraseOut(BaseModel):
    domain: str
    kind: str
    mode: Literal["record", "value"]
    erased: int = Field(description="Vault values erased by this request.")


@router.post(
    "/domains/{domain_id}/vault/erase",
    summary="Erase personal data from the vault",
    response_model=EraseOut,
    responses=_ERRORS,
)
async def erase(
    domain_id: str, body: EraseIn, access: Erase, store: StoreDep, request: Request
) -> EraseOut:
    """Erase the vaulted values of a Record or a value in a Kind, on every Revision."""
    annotate(request, action=ERASE_ACTION, target=body.kind)
    async with store.sessions() as session, session.begin():
        domain = await require_domain(session, domain_id)
        branch = body.branch or domain.default_branch
        compiled = await MaterializedRepository(
            session, domain_id, branch, writes=store.runtime.settings.writes
        ).schema()
        if compiled is None:
            raise ProblemError(NOT_FOUND, f"Branch {branch!r} not found or not materialized.")
        schema = load_compiled(compiled.compiled)
        kind = _kind(schema, body.kind)
        at = store.clock()
        if body.key is not None:
            attribute_ids = (
                None
                if body.attributes is None
                else [
                    _vault_attribute(kind, name, f"/body/attributes/{index}")
                    for index, name in enumerate(body.attributes)
                ]
            )
            erased = await erase_record(session, domain_id, kind.id, body.key, attribute_ids, at=at)
            mode: Literal["record", "value"] = "record"
            target = f"{kind.api_name}/{body.key}"
            details: dict[str, Any] = {"kind": kind.id, "key": body.key}
            if body.attributes is not None:
                details["attributes"] = attribute_ids
        else:
            assert body.attribute is not None
            attribute_id = _vault_attribute(kind, body.attribute, "/body/attribute")
            keys = store.runtime.vault
            if keys is None:
                raise ProblemError(
                    CONFLICT, "Erasure by value needs vault.token_key and vault.encryption_key."
                )
            try:
                canonical = encode_value(kind.attributes[attribute_id].type, body.value)
            except EncodingError as error:
                raise invalid("/body/value", str(error), "value_invalid") from None
            fingerprint = keys.fingerprint(domain_id, kind.id, attribute_id, canonical)
            erased = await erase_matching(
                session, domain_id, kind.id, attribute_id, fingerprint, at=at
            )
            mode = "value"
            target = f"{kind.api_name}.{body.attribute}"
            details = {"kind": kind.id, "attribute": attribute_id}
        details["erased"] = erased
        await AuditRepository(session).append(
            NewAuditEntry(
                actor=access.principal.id,
                action=ERASE_ACTION,
                domain_id=domain_id,
                target=target,
                details={"mode": mode, **details},
                at=at,
            )
        )
    # The entry above is the audit record of the erasure; the request's own entry would repeat it.
    suppress(request)
    return EraseOut(domain=domain_id, kind=kind.api_name, mode=mode, erased=erased)


def _kind(schema: Schema, name: str) -> Kind:
    kind = schema.kind_by_api_name(name)
    if kind is None:
        raise ProblemError(NOT_FOUND, f"Kind {name} not found.")
    return kind


def _vault_attribute(kind: Kind, name: str, pointer: str) -> str:
    attribute = kind.attribute_by_api_name(name)
    if attribute is None or not attribute.in_vault:
        raise invalid(pointer, f"{name} is not a vault Attribute of {kind.api_name}", "not_vault")
    return attribute.id
