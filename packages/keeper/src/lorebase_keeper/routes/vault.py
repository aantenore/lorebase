"""Erasure of personal data held in the vault (design 13.5, AC-22).

`POST /domains/{d}/vault/erase` erases the vaulted values of one data subject, needing
`pii.erase` on the whole Domain (only `admin` holds it among the built-in roles):

- by Record: `kind` and `key`, optionally limited to some vault `attributes`; every value the
  Record ever held in them, under every key it had (key changes), on every branch and Revision;
- by value: `kind`, a vault `attribute` and the `value`; every Record of the Kind holding it
  or a case variant of it (same matching form, `lorebase_core.types.matching`), on every branch
  and Revision.

Revisions keep their hashes and structure: Record objects hold tokens, and reads of an erased
token answer `keeper.reads.sensitivity.erased_value`. Erasing again changes nothing (`erased`
counts the values erased by this request). A later write of an erased value is stored erased,
refused or stored again, as `vault.erased_values` says. The erasure and its audit entry (action
`vault.erase`, with the Kind, a keyed digest of the key (`lorebase_store.digests`) or the
Attribute, and the count, never the key or the value) commit in one transaction. Attribute and
Kind names are read in the schema of `branch` (default: the Domain's default branch).

Vault keys rotate (`lorebase_store.vault.rotation`), under `vault.manage`, which installation
administrators hold: `GET /vault/keys` counts the rows stored under each encryption key and
fingerprint key and tells which earlier keys no row uses any more; `POST /vault/rotate` queues
the `vault.rotate` job that makes the outdated rows of every Domain again under the current
keys, in batches, continuing where it stopped.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, JsonValue, model_validator

from lorebase_core.model import Kind, Schema
from lorebase_core.types import EncodingError, encode_value
from lorebase_keeper.auth.audit import annotate, suppress
from lorebase_keeper.auth.deps import Access, require, require_installation
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.context import StoreDep, require_domain
from lorebase_keeper.errors import (
    CONFLICT,
    NOT_FOUND,
    UNAVAILABLE,
    Problem,
    ProblemError,
    ValidationProblem,
)
from lorebase_keeper.reading.problems import invalid
from lorebase_store.auth import AuditRepository, NewAuditEntry
from lorebase_store.commit.schema import load_compiled
from lorebase_store.jobs import VAULT_ROTATE_JOB, enqueue_job
from lorebase_store.repositories import MaterializedRepository
from lorebase_store.vault import erase_matching, erase_record, usage
from lorebase_store.vault.store import VaultLockTimeoutError

router = APIRouter(tags=["vault"])
log = logging.getLogger("lorebase.keeper.vault")

ERASE_ACTION = "vault.erase"
KEY_DIGEST = "erased_record_key"
"""Purpose of the keyed digest of `<kind id>/<key>` held by the audit entry of an erasure."""

_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks pii.erase on the Domain."},
    404: {"model": Problem, "description": "Domain, branch or Kind not found."},
    409: {"model": Problem, "description": "Erasure by value needs the vault keys."},
    503: {"model": Problem, "description": "The vault of the Domain stayed locked."},
    422: {"model": ValidationProblem, "description": "Invalid request."},
}

Erase = Annotated[Access, Depends(require(Permission.PII_ERASE))]
Manage = Annotated[Access, Depends(require_installation(Permission.VAULT_MANAGE))]

ROTATE_ACTION = "vault.rotate"

_KEY_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller is not an installation administrator."},
    409: {"model": Problem, "description": "The vault is not configured."},
}


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
    unopenable: int = Field(
        default=0,
        description="Rows looked at that are sealed under a key no longer configured: their "
        "fingerprints could not be made again, so a case variant among them may be missed.",
    )


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
    try:
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
                erasure = await erase_record(
                    session,
                    domain_id,
                    kind.id,
                    body.key,
                    attribute_ids,
                    keys=store.runtime.vault,
                    at=at,
                    lock_timeout_ms=store.runtime.vault_settings.erase_lock_timeout_ms,
                )
                mode: Literal["record", "value"] = "record"
                digests = await store.runtime.digests.ready(session)
                key_digest = digests.digest(domain_id, KEY_DIGEST, f"{kind.id}/{body.key}".encode())
                target = f"{kind.api_name}/#{key_digest[:16]}"
                details: dict[str, Any] = {"kind": kind.id, "key_digest": key_digest}
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
                erasure = await erase_matching(
                    session,
                    domain_id,
                    kind.id,
                    attribute_id,
                    canonical,
                    keys,
                    at=at,
                    lock_timeout_ms=store.runtime.vault_settings.erase_lock_timeout_ms,
                )
                mode = "value"
                target = f"{kind.api_name}.{body.attribute}"
                details = {"kind": kind.id, "attribute": attribute_id}
            details["erased"] = erasure.erased
            if erasure.unopenable:
                details["unopenable"] = erasure.unopenable
            if erasure.linked:
                # Flagged for an administrator: the subject's keys were changed unusually often.
                details["linked_keys"] = erasure.keys
                log.warning(
                    "erasure followed many key changes",
                    extra={"domain_id": domain_id, "kind": kind.id, "linked_keys": erasure.keys},
                )
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
    except VaultLockTimeoutError as error:
        raise ProblemError(
            UNAVAILABLE,
            f"{error}; retry later.",
            headers={"Retry-After": "5"},
        ) from None
    # The entry above is the audit record of the erasure; the request's own entry would repeat it.
    suppress(request)
    return EraseOut(
        domain=domain_id,
        kind=kind.api_name,
        mode=mode,
        erased=erasure.erased,
        unopenable=erasure.unopenable,
    )


class VaultKeysOut(BaseModel):
    """Which keys the vault rows use."""

    key_id: str = Field(description="Encryption key new values are sealed under.")
    token_key_id: str = Field(description="Token key new tokens and fingerprints use.")
    fingerprint_key: str = Field(description="Current fingerprint key: token key id/scheme.")
    encryption_keys: dict[str, int] = Field(description="Live rows by encryption key id.")
    fingerprint_keys: dict[str, int] = Field(
        description="Live rows by fingerprint key (`legacy`: written before rotation)."
    )
    tombstone_keys: dict[str, int] = Field(
        description="Fingerprints of erased values by fingerprint key."
    )
    outdated: int = Field(description="Live rows the rotation job still has to make again.")
    removable_keys: list[str] = Field(
        description="Ids in vault.previous_keys that no row uses: they can be removed."
    )
    removable_token_keys: list[str] = Field(
        description="Ids in vault.previous_token_keys that no row or tombstone uses."
    )
    digests: str = Field(
        description="Key of the keyed digests: vault.digest_key, vault.token_key or "
        "installation (the database's own secret)."
    )


class RotationQueued(BaseModel):
    job_id: int = Field(description="Id of the queued (or already pending) rotation job.")


@router.get(
    "/vault/keys",
    summary="Show which keys the vault rows use",
    response_model=VaultKeysOut,
    responses=_KEY_ERRORS,
)
async def vault_keys(access: Manage, store: StoreDep) -> VaultKeysOut:
    """Rows by encryption key and fingerprint key, and the earlier keys that can be removed."""
    keys = store.runtime.vault
    if keys is None:
        raise ProblemError(CONFLICT, "The vault is not configured.")
    settings = store.runtime.vault_settings
    async with store.sessions() as session, session.begin():
        found = await usage(session)
    token_ids_in_use = {k.split("/", 1)[0] for k in found.fingerprint_keys} | {
        k.split("/", 1)[0] for k in found.tombstone_keys
    }
    return VaultKeysOut(
        key_id=keys.key_id,
        token_key_id=keys.token_key_id,
        fingerprint_key=keys.fingerprint_key,
        encryption_keys=found.encryption_keys,
        fingerprint_keys=found.fingerprint_keys,
        tombstone_keys=found.tombstone_keys,
        outdated=found.outdated(keys),
        removable_keys=sorted(k for k in settings.previous_keys if k not in found.encryption_keys),
        removable_token_keys=sorted(
            k
            for k in settings.previous_token_keys
            if k not in token_ids_in_use and "legacy" not in token_ids_in_use
        ),
        digests=store.runtime.digests.source,
    )


@router.post(
    "/vault/rotate",
    status_code=202,
    summary="Queue a vault key rotation",
    response_model=RotationQueued,
    responses=_KEY_ERRORS,
)
async def rotate_vault(access: Manage, store: StoreDep, request: Request) -> RotationQueued:
    """Queue the job that makes the rows stored under earlier keys again under the current
    ones."""
    if store.runtime.vault is None:
        raise ProblemError(CONFLICT, "The vault is not configured.")
    async with store.sessions() as session, session.begin():
        job_id = await enqueue_job(session, VAULT_ROTATE_JOB, payload={}, unique=True)
    annotate(request, action=ROTATE_ACTION, job=job_id)
    return RotationQueued(job_id=job_id)


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
