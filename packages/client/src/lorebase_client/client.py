"""Synchronous client: `LorebaseClient`.

    with LorebaseClient("http://localhost:8080", api_key="lbk_...") as client:
        client.domains.create("supply", display_name="Supply chain")
        for record in client.records.iterate("supply", "Supplier", branch="work"):
            print(record.key, record.title)

Resources group the endpoints (`domains`, `branches`, `tags`, `schema`, `operations`, `records`,
`revisions`, `api_keys`, `grants`, `principals`, `audit`, `proposals`, `agents`, `webhooks`,
`sources`).
Every list endpoint has a `list` method (one page) and an `iterate` method (all items; pages
are fetched on demand). Errors
raise subclasses of `LorebaseError`. The request layer is shared with `AsyncLorebaseClient`
(`lorebase_client.core`).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from types import TracebackType
from typing import Any, Self

import httpx
from pydantic import JsonValue

from lorebase_client import endpoints as ep
from lorebase_client import models as m
from lorebase_client.config import ClientConfig, RetryPolicy, TokenSource, resolve_config
from lorebase_client.core import (
    Call,
    Done,
    Exchange,
    RequestCore,
    Retry,
    new_idempotency_key,
)

JsonObject = Mapping[str, Any]


class LorebaseClient:
    """Blocking client over `httpx.Client`.

    `base_url`, `api_key`, `token` and `timeout` fall back to `LOREBASE_URL`,
    `LOREBASE_API_KEY`, `LOREBASE_TOKEN` and `LOREBASE_TIMEOUT`. `cache=True` keeps an ETag
    cache for GET reads (an integer sets its size). `auto_idempotency_keys=True` gives every
    create and Operation execution a key, which also makes them safe to retry after a lost
    answer. `transport` replaces the network (tests, in-process servers); `sleep` replaces the
    wait between retries.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        api_key: str | None = None,
        token: TokenSource | None = None,
        timeout: float | None = None,
        api_prefix: str | None = None,
        retry: RetryPolicy | None = None,
        auto_idempotency_keys: bool = False,
        cache: bool | int = False,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        env: Mapping[str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config: ClientConfig = resolve_config(
            base_url,
            api_key=api_key,
            token=token,
            timeout=timeout,
            api_prefix=api_prefix,
            retry=retry,
            auto_idempotency_keys=auto_idempotency_keys,
            cache=cache,
            headers=headers,
            env=env,
        )
        self._core = RequestCore(self.config)
        self._sleep = sleep
        self._http = httpx.Client(timeout=self.config.timeout_s, transport=transport)
        self.domains = Domains(self)
        self.branches = Branches(self)
        self.tags = Tags(self)
        self.schema = SchemaApi(self)
        self.operations = Operations(self)
        self.records = Records(self)
        self.revisions = Revisions(self)
        self.api_keys = ApiKeys(self)
        self.grants = Grants(self)
        self.principals = Principals(self)
        self.audit = Audit(self)
        self.proposals = Proposals(self)
        self.agents = Agents(self)
        self.webhooks = Webhooks(self)
        self.sources = Sources(self)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # Request layer ---------------------------------------------------------------------

    def send[T](self, call: Call[T]) -> T:
        """Run a call, retrying as the retry policy says; returns the parsed answer."""
        exchange = Exchange(self._core, call)
        step: Done[T] | Retry
        while True:
            request = self._http.build_request(**exchange.request_args())
            try:
                response = self._http.send(request)
            except httpx.TransportError as error:
                step = exchange.on_error(error)
            else:
                step = exchange.on_response(response)
            if isinstance(step, Done):
                return step.value
            self._sleep(step.delay)

    def pages[P: m.Paged[Any]](self, call: Call[P]) -> Iterator[P]:
        """Every page of a list call, following `next_cursor`."""
        cursor = call.params.get("cursor")
        while True:
            page = self.send(call.with_params(cursor=cursor))
            yield page
            cursor = page.next_cursor
            if cursor is None:
                return

    def iterate[T](self, call: Call[m.Paged[T]]) -> Iterator[T]:
        """Every item of a list call."""
        for page in self.pages(call):
            yield from page.items

    def whoami(self) -> m.WhoAmI:
        """The authenticated caller."""
        return self.send(ep.whoami())

    def idempotency_key(self, prefix: str | None = None) -> str:
        """A fresh key to pass as `idempotency_key=` to a write."""
        return new_idempotency_key(prefix)

    def cache_clear(self) -> None:
        """Forget cached answers (no effect when the cache is off)."""
        if self._core.cache is not None:
            self._core.cache.clear()


class _Resource:
    def __init__(self, client: LorebaseClient) -> None:
        self._c = client


class Domains(_Resource):
    def create(
        self,
        id: str,
        *,
        display_name: str = "",
        description: str = "",
        default_branch: str | None = None,
        settings: JsonObject | None = None,
        idempotency_key: str | None = None,
    ) -> m.Domain:
        body = m.DomainCreate(
            id=id,
            display_name=display_name,
            description=description,
            default_branch=default_branch,
            settings=dict(settings or {}),
        )
        return self._c.send(ep.create_domain(body, idempotency_key=idempotency_key))

    def list(self, *, limit: int | None = None, cursor: str | None = None) -> m.Page[m.Domain]:
        return self._c.send(ep.list_domains(limit=limit, cursor=cursor))

    def iterate(self, *, page_size: int | None = None) -> Iterator[m.Domain]:
        return self._c.iterate(ep.list_domains(limit=page_size))

    def get(self, id: str) -> m.Domain:
        return self._c.send(ep.get_domain(id))

    def update(
        self, id: str, *, display_name: str | None = None, description: str | None = None
    ) -> m.Domain:
        body = m.DomainUpdate(display_name=display_name, description=description)
        return self._c.send(ep.update_domain(id, body))

    def delete(self, id: str, *, confirm: str) -> None:
        """Delete the Domain with everything in it; `confirm` repeats its id."""
        self._c.send(ep.delete_domain(id, confirm=confirm))


class Branches(_Resource):
    def list(
        self, domain: str, *, limit: int | None = None, cursor: str | None = None
    ) -> m.Page[m.Branch]:
        return self._c.send(ep.list_branches(domain, limit=limit, cursor=cursor))

    def iterate(self, domain: str, *, page_size: int | None = None) -> Iterator[m.Branch]:
        return self._c.iterate(ep.list_branches(domain, limit=page_size))

    def create(
        self,
        domain: str,
        name: str,
        *,
        source: str | None = None,
        revision: str | None = None,
        idempotency_key: str | None = None,
    ) -> m.Branch:
        body = m.BranchCreate(name=name, source=source, revision=revision)
        return self._c.send(ep.create_branch(domain, body, idempotency_key=idempotency_key))

    def get(self, domain: str, name: str) -> m.BranchDetail:
        return self._c.send(ep.get_branch(domain, name))

    def set_protected(self, domain: str, name: str, protected: bool) -> m.Branch:
        return self._c.send(ep.protect_branch(domain, name, protected=protected))

    def delete(self, domain: str, name: str) -> None:
        self._c.send(ep.delete_branch(domain, name))


class Tags(_Resource):
    def list(
        self, domain: str, *, limit: int | None = None, cursor: str | None = None
    ) -> m.Page[m.Tag]:
        return self._c.send(ep.list_tags(domain, limit=limit, cursor=cursor))

    def iterate(self, domain: str, *, page_size: int | None = None) -> Iterator[m.Tag]:
        return self._c.iterate(ep.list_tags(domain, limit=page_size))

    def create(
        self,
        domain: str,
        name: str,
        *,
        source: str | None = None,
        revision: str | None = None,
        idempotency_key: str | None = None,
    ) -> m.Tag:
        body = m.TagCreate(name=name, source=source, revision=revision)
        return self._c.send(ep.create_tag(domain, body, idempotency_key=idempotency_key))


class SchemaApi(_Resource):
    def get(
        self, domain: str, *, branch: str | None = None, at: str | None = None
    ) -> m.SchemaDocument:
        return self._c.send(ep.get_schema(domain, branch=branch, at=at))

    def get_yaml(self, domain: str, *, branch: str | None = None, at: str | None = None) -> str:
        return self._c.send(ep.get_schema_yaml(domain, branch=branch, at=at))

    def element(
        self,
        domain: str,
        section: m.SchemaSection,
        name: str,
        *,
        branch: str | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        return self._c.send(ep.get_schema_element(domain, section, name, branch=branch, at=at))

    def validate(
        self,
        domain: str,
        *,
        yaml: str | None = None,
        patch: JsonObject | None = None,
        remove: Sequence[m.ElementRemoval] | None = None,
        branch: str | None = None,
        at: str | None = None,
    ) -> m.SchemaValidation:
        body = m.SchemaValidateRequest(
            yaml=yaml,
            patch=None if patch is None else dict(patch),
            remove=list(remove or []),
            branch=branch,
            at=at,
        )
        return self._c.send(ep.validate_schema(domain, body))

    def apply(
        self,
        domain: str,
        branch: str,
        *,
        yaml: str | None = None,
        patch: JsonObject | None = None,
        remove: Sequence[m.ElementRemoval] | None = None,
        message: str | None = None,
        expected_head: str | None = None,
        dry_run: bool = False,
        cascade: bool = False,
    ) -> m.SchemaChangeResult:
        body = m.SchemaChangeRequest(
            yaml=yaml,
            patch=None if patch is None else dict(patch),
            remove=list(remove or []),
            message=message,
            expected_head=expected_head,
            dry_run=dry_run,
            cascade=cascade,
        )
        return self._c.send(ep.apply_schema(domain, branch, body))

    def infer_postgres(
        self,
        domain: str,
        dsn: str,
        *,
        schema: str | None = None,
        tables: Sequence[str] | None = None,
        options: JsonObject | None = None,
    ) -> dict[str, Any]:
        """Propose a schema from Postgres tables (`dsn` may be a `secret://` reference)."""
        return self._c.send(
            ep.infer_schema_postgres(domain, _infer_body(dsn, schema, tables, options))
        )

    def infer_files(
        self, domain: str, files: Mapping[str, bytes], *, options: JsonObject | None = None
    ) -> dict[str, Any]:
        """Propose a schema from CSV or TSV files (file name to content)."""
        return self._c.send(ep.infer_schema_files(domain, files, options=options))


class Operations(_Resource):
    def execute(
        self,
        domain: str,
        operation: str,
        parameters: JsonObject | None = None,
        *,
        branch: str | None = None,
        dry_run: bool = False,
        expected_head: str | None = None,
        message: str | None = None,
        idempotency_key: str | None = None,
    ) -> m.Execution:
        body = m.ExecuteRequest(
            parameters=dict(parameters or {}),
            branch=branch,
            dry_run=dry_run,
            expected_head=expected_head,
            message=message,
        )
        return self._c.send(
            ep.execute_operation(domain, operation, body, idempotency_key=idempotency_key)
        )

    def execute_as_agent(
        self,
        domain: str,
        operation: str,
        parameters: JsonObject | None = None,
        *,
        task: str,
        intent: str | None = None,
        auto_propose: bool | None = None,
        auto_merge: bool | None = None,
        branch: str | None = None,
        expected_head: str | None = None,
        message: str | None = None,
        idempotency_key: str | None = None,
    ) -> m.Execution | m.AgentWrite:
        """Execute an Operation that may need review. When it does (protected branch, review
        policy) it runs on the branch `agent/{principal}/{task}` and the answer is an
        `AgentWrite` naming the Proposal; otherwise it is the usual `Execution`."""
        body = m.ExecuteRequest(
            parameters=dict(parameters or {}),
            branch=branch,
            expected_head=expected_head,
            message=message,
            task=task,
            intent=intent,
            auto_propose=auto_propose,
            auto_merge=auto_merge,
        )
        return self._c.send(
            ep.execute_operation_as_agent(domain, operation, body, idempotency_key=idempotency_key)
        )

    def import_records(
        self,
        domain: str,
        kind: str,
        rows: Sequence[JsonObject],
        *,
        branch: str | None = None,
        dry_run: bool = False,
        expected_head: str | None = None,
        message: str | None = None,
        idempotency_key: str | None = None,
    ) -> m.Execution:
        """Upsert Records of `kind`; rows are objects keyed by Attribute api name."""
        return self.execute(
            domain,
            "import_records",
            {"kind": kind, "rows": list(rows)},
            branch=branch,
            dry_run=dry_run,
            expected_head=expected_head,
            message=message,
            idempotency_key=idempotency_key,
        )

    def import_links(
        self,
        domain: str,
        relation: str,
        rows: Sequence[JsonObject],
        *,
        branch: str | None = None,
        dry_run: bool = False,
        expected_head: str | None = None,
        message: str | None = None,
        idempotency_key: str | None = None,
    ) -> m.Execution:
        """Upsert Links of a forward Relation; rows have `from`, `to` and Link Attributes."""
        return self.execute(
            domain,
            "import_links",
            {"relation": relation, "rows": list(rows)},
            branch=branch,
            dry_run=dry_run,
            expected_head=expected_head,
            message=message,
            idempotency_key=idempotency_key,
        )

    def import_batch(
        self,
        domain: str,
        records: Sequence[JsonObject] = (),
        links: Sequence[JsonObject] = (),
        *,
        branch: str | None = None,
        dry_run: bool = False,
        expected_head: str | None = None,
        message: str | None = None,
        idempotency_key: str | None = None,
    ) -> m.Execution:
        """Upsert Records (rows with `kind`) and Links (rows with `relation`, `from`, `to`) in
        one call; a Link goes in the same Revision as the Record at one of its ends."""
        return self.execute(
            domain,
            "import_batch",
            {"records": list(records), "links": list(links)},
            branch=branch,
            dry_run=dry_run,
            expected_head=expected_head,
            message=message,
            idempotency_key=idempotency_key,
        )


class Records(_Resource):
    def list(
        self,
        domain: str,
        kind: str,
        *,
        branch: str | None = None,
        at: str | None = None,
        where: JsonObject | None = None,
        order_by: Sequence[str] | None = None,
        select: Sequence[str] | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> m.RecordPage:
        """One page of Records; `order_by` terms are `name` or `name:desc`."""
        return self._c.send(
            ep.list_records(
                domain,
                kind,
                branch=branch,
                at=at,
                where=where,
                order_by=order_by,
                select=select,
                limit=limit,
                cursor=cursor,
            )
        )

    def iterate(
        self,
        domain: str,
        kind: str,
        *,
        branch: str | None = None,
        at: str | None = None,
        where: JsonObject | None = None,
        order_by: Sequence[str] | None = None,
        select: Sequence[str] | None = None,
        page_size: int | None = None,
    ) -> Iterator[m.RecordItem]:
        call = ep.list_records(
            domain,
            kind,
            branch=branch,
            at=at,
            where=where,
            order_by=order_by,
            select=select,
            limit=page_size,
        )
        return self._c.iterate(call)

    def get(
        self,
        domain: str,
        kind: str,
        key: str,
        *,
        branch: str | None = None,
        at: str | None = None,
        links: bool = False,
        lineage: bool = False,
    ) -> m.RecordResponse:
        return self._c.send(
            ep.get_record(domain, kind, key, branch=branch, at=at, links=links, lineage=lineage)
        )

    def links(
        self,
        domain: str,
        kind: str,
        key: str,
        relation: str,
        *,
        branch: str | None = None,
        at: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> m.LinkPage:
        """Links of one Record along a Relation (forward or inverse api name)."""
        return self._c.send(
            ep.list_record_links(
                domain, kind, key, relation, branch=branch, at=at, limit=limit, cursor=cursor
            )
        )

    def relation_links(
        self,
        domain: str,
        relation: str,
        *,
        branch: str | None = None,
        at: str | None = None,
        from_key: str | None = None,
        to_key: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> m.LinkPage:
        return self._c.send(
            ep.list_relation_links(
                domain,
                relation,
                branch=branch,
                at=at,
                from_key=from_key,
                to_key=to_key,
                limit=limit,
                cursor=cursor,
            )
        )

    def iterate_relation_links(
        self,
        domain: str,
        relation: str,
        *,
        branch: str | None = None,
        at: str | None = None,
        from_key: str | None = None,
        to_key: str | None = None,
        page_size: int | None = None,
    ) -> Iterator[m.LinkItem]:
        call = ep.list_relation_links(
            domain,
            relation,
            branch=branch,
            at=at,
            from_key=from_key,
            to_key=to_key,
            limit=page_size,
        )
        return self._c.iterate(call)

    def query(self, domain: str, query: m.QueryRequest | JsonObject) -> m.QueryResult:
        """Run a DSL query (a `QueryRequest` or the JSON object of the DSL)."""
        return self._c.send(ep.query(domain, _query_request(query)))

    def iterate_query(
        self, domain: str, query: m.QueryRequest | JsonObject
    ) -> Iterator[m.RecordItem]:
        """All Records a query matches, page after page."""
        request = _query_request(query)
        while True:
            page = self._c.send(ep.query(domain, request))
            yield from page.items
            if page.next_cursor is None:
                return
            request = request.model_copy(update={"cursor": page.next_cursor})

    def traverse(self, domain: str, request: m.TraverseRequest | JsonObject) -> m.TraverseResult:
        body = request if isinstance(request, m.TraverseRequest) else m.TraverseRequest(**request)
        return self._c.send(ep.traverse(domain, body))

    def neighborhood(
        self, domain: str, request: m.NeighborhoodRequest | JsonObject
    ) -> m.NeighborhoodResult:
        body = (
            request
            if isinstance(request, m.NeighborhoodRequest)
            else m.NeighborhoodRequest(**request)
        )
        return self._c.send(ep.neighborhood(domain, body))

    def search(self, domain: str, request: m.SearchRequest | JsonObject) -> m.SearchResult:
        body = request if isinstance(request, m.SearchRequest) else m.SearchRequest(**request)
        return self._c.send(ep.search(domain, body))


def _query_request(query: m.QueryRequest | JsonObject) -> m.QueryRequest:
    return query if isinstance(query, m.QueryRequest) else m.QueryRequest(**query)


def _context_request(request: m.ContextRequest | JsonObject) -> m.ContextRequest:
    return request if isinstance(request, m.ContextRequest) else m.ContextRequest(**request)


def _infer_body(
    dsn: str, schema: str | None, tables: Sequence[str] | None, options: JsonObject | None
) -> dict[str, Any]:
    postgres: dict[str, Any] = {"dsn": dsn}
    if schema is not None:
        postgres["schema"] = schema
    if tables is not None:
        postgres["tables"] = list(tables)
    body: dict[str, Any] = {"postgres": postgres}
    if options:
        body["options"] = dict(options)
    return body


class Revisions(_Resource):
    def log(
        self,
        domain: str,
        branch: str,
        *,
        all_parents: bool = False,
        origin_type: str | None = None,
        author: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> m.LogPage:
        return self._c.send(
            ep.branch_log(
                domain,
                branch,
                all_parents=all_parents,
                origin_type=origin_type,
                author=author,
                limit=limit,
                cursor=cursor,
            )
        )

    def iterate_log(
        self,
        domain: str,
        branch: str,
        *,
        all_parents: bool = False,
        origin_type: str | None = None,
        author: str | None = None,
        page_size: int | None = None,
    ) -> Iterator[m.Revision]:
        call = ep.branch_log(
            domain,
            branch,
            all_parents=all_parents,
            origin_type=origin_type,
            author=author,
            limit=page_size,
        )
        return self._c.iterate(call)

    def get(self, domain: str, revision: str) -> m.Revision:
        """A Revision by id, tag or branch name."""
        return self._c.send(ep.get_revision(domain, revision))

    def counts(self, domain: str, revision: str) -> m.RevisionCounts:
        return self._c.send(ep.revision_counts(domain, revision))

    def changeset(
        self,
        domain: str,
        revision: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> m.ChangesPage:
        return self._c.send(ep.revision_changeset(domain, revision, limit=limit, cursor=cursor))

    def iterate_changeset(
        self, domain: str, revision: str, *, page_size: int | None = None
    ) -> Iterator[m.Change]:
        return self._c.iterate(ep.revision_changeset(domain, revision, limit=page_size))

    def diff(
        self,
        domain: str,
        from_: str,
        to: str,
        *,
        kinds: Sequence[str] | None = None,
        relations: Sequence[str] | None = None,
        schema: bool | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> m.ChangesPage:
        return self._c.send(
            ep.diff(
                domain,
                from_,
                to,
                kinds=kinds,
                relations=relations,
                schema=schema,
                limit=limit,
                cursor=cursor,
            )
        )

    def iterate_diff(
        self,
        domain: str,
        from_: str,
        to: str,
        *,
        kinds: Sequence[str] | None = None,
        relations: Sequence[str] | None = None,
        schema: bool | None = None,
        page_size: int | None = None,
    ) -> Iterator[m.Change]:
        call = ep.diff(
            domain, from_, to, kinds=kinds, relations=relations, schema=schema, limit=page_size
        )
        return self._c.iterate(call)


class ApiKeys(_Resource):
    def create(
        self,
        domain: str,
        name: str,
        *,
        role: str | None = None,
        ttl_days: int | None = None,
        principal_id: str | None = None,
    ) -> m.ApiKeyCreated:
        """Create a key; the secret is in `key` of the answer and is not shown again."""
        body = m.CreateApiKey(name=name, role=role, ttl_days=ttl_days, principal_id=principal_id)
        return self._c.send(ep.create_api_key(domain, body))

    def list(
        self, domain: str, *, limit: int | None = None, cursor: str | None = None
    ) -> m.Page[m.ApiKey]:
        return self._c.send(ep.list_api_keys(domain, limit=limit, cursor=cursor))

    def iterate(self, domain: str, *, page_size: int | None = None) -> Iterator[m.ApiKey]:
        return self._c.iterate(ep.list_api_keys(domain, limit=page_size))

    def revoke(self, domain: str, key_id: str) -> None:
        self._c.send(ep.revoke_api_key(domain, key_id))


class Grants(_Resource):
    def create(
        self,
        domain: str,
        subject: str,
        role: str,
        *,
        scope_type: str = "domain",
        scope_ref: str | None = None,
    ) -> m.Grant:
        body = m.CreateGrant.model_validate(
            {"subject": subject, "role": role, "scope_type": scope_type, "scope_ref": scope_ref}
        )
        return self._c.send(ep.create_grant(domain, body))

    def list(
        self, domain: str, *, limit: int | None = None, cursor: str | None = None
    ) -> m.Page[m.Grant]:
        return self._c.send(ep.list_grants(domain, limit=limit, cursor=cursor))

    def iterate(self, domain: str, *, page_size: int | None = None) -> Iterator[m.Grant]:
        return self._c.iterate(ep.list_grants(domain, limit=page_size))

    def revoke(self, domain: str, grant_id: str) -> None:
        self._c.send(ep.revoke_grant(domain, grant_id))


class Principals(_Resource):
    def list(
        self, *, type_: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> m.Page[m.Principal]:
        return self._c.send(ep.list_principals(type_=type_, limit=limit, cursor=cursor))

    def iterate(
        self, *, type_: str | None = None, page_size: int | None = None
    ) -> Iterator[m.Principal]:
        return self._c.iterate(ep.list_principals(type_=type_, limit=page_size))


class Audit(_Resource):
    def list(
        self, domain: str, *, limit: int | None = None, cursor: str | None = None
    ) -> m.Page[m.AuditEntry]:
        return self._c.send(ep.list_audit(domain, limit=limit, cursor=cursor))

    def iterate(self, domain: str, *, page_size: int | None = None) -> Iterator[m.AuditEntry]:
        return self._c.iterate(ep.list_audit(domain, limit=page_size))


class Proposals(_Resource):
    def create(
        self,
        domain: str,
        source_ref: str,
        target_ref: str,
        *,
        title: str,
        description: str | None = None,
        idempotency_key: str | None = None,
    ) -> m.ProposalDetail:
        """Propose merging `source_ref` into `target_ref`."""
        body = m.ProposalCreate(
            source_ref=source_ref, target_ref=target_ref, title=title, description=description
        )
        return self._c.send(ep.create_proposal(domain, body, idempotency_key=idempotency_key))

    def list(
        self,
        domain: str,
        *,
        status: m.ProposalStatus | None = None,
        origin: m.ProposalOrigin | None = None,
        source: str | None = None,
        target: str | None = None,
        author: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> m.Page[m.Proposal]:
        return self._c.send(
            ep.list_proposals(
                domain,
                status=status,
                origin=origin,
                source=source,
                target=target,
                author=author,
                limit=limit,
                cursor=cursor,
            )
        )

    def iterate(
        self,
        domain: str,
        *,
        status: m.ProposalStatus | None = None,
        origin: m.ProposalOrigin | None = None,
        source: str | None = None,
        target: str | None = None,
        author: str | None = None,
        page_size: int | None = None,
    ) -> Iterator[m.Proposal]:
        call = ep.list_proposals(
            domain,
            status=status,
            origin=origin,
            source=source,
            target=target,
            author=author,
            limit=page_size,
        )
        return self._c.iterate(call)

    def get(self, domain: str, proposal_id: str) -> m.ProposalDetail:
        return self._c.send(ep.get_proposal(domain, proposal_id))

    def update(
        self,
        domain: str,
        proposal_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> m.ProposalDetail:
        body = m.ProposalUpdate(title=title, description=description)
        return self._c.send(ep.update_proposal(domain, proposal_id, body))

    def close(self, domain: str, proposal_id: str) -> m.ProposalDetail:
        return self._c.send(ep.close_proposal(domain, proposal_id))

    def review(
        self,
        domain: str,
        proposal_id: str,
        verdict: m.ReviewVerdict,
        *,
        comment: str | None = None,
    ) -> m.ReviewResult:
        body = m.ReviewIn(verdict=verdict, comment=comment)
        return self._c.send(ep.review_proposal(domain, proposal_id, body))

    def merge(
        self,
        domain: str,
        proposal_id: str,
        *,
        message: str | None = None,
        idempotency_key: str | None = None,
    ) -> m.MergeResult:
        body = m.MergeIn(message=message)
        return self._c.send(
            ep.merge_proposal(domain, proposal_id, body, idempotency_key=idempotency_key)
        )

    def conflicts(
        self,
        domain: str,
        proposal_id: str,
        *,
        kind: str | None = None,
        status: m.ConflictStatus | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> m.Conflicts:
        """One page of the Conflicts of the latest merge attempt; `kind` is a code list."""
        return self._c.send(
            ep.list_conflicts(
                domain, proposal_id, kind=kind, status=status, limit=limit, cursor=cursor
            )
        )

    def iterate_conflicts(
        self,
        domain: str,
        proposal_id: str,
        *,
        kind: str | None = None,
        status: m.ConflictStatus | None = None,
        page_size: int | None = None,
    ) -> Iterator[m.ConflictItem]:
        call = ep.list_conflicts(domain, proposal_id, kind=kind, status=status, limit=page_size)
        return self._c.iterate(call)

    def resolve(
        self,
        domain: str,
        proposal_id: str,
        conflict_id: str,
        choice: str,
        *,
        value: JsonValue = None,
        values: JsonObject | None = None,
    ) -> m.Resolution:
        """Decide one Conflict; `value` or `values` go with the `custom` choice."""
        body = m.ResolutionIn(
            choice=choice, value=value, values=None if values is None else dict(values)
        )
        return self._c.send(ep.resolve_conflict(domain, proposal_id, conflict_id, body))

    def resolve_many(
        self,
        domain: str,
        proposal_id: str,
        resolutions: Sequence[m.FingerprintedResolutionIn | JsonObject],
    ) -> m.Conflicts:
        """Decide several Conflicts, each named by its fingerprint."""
        body = m.ResolveManyIn(
            resolutions=[
                r
                if isinstance(r, m.FingerprintedResolutionIn)
                else m.FingerprintedResolutionIn(**r)
                for r in resolutions
            ]
        )
        return self._c.send(ep.resolve_conflicts(domain, proposal_id, body))


class Agents(_Resource):
    def context(self, domain: str, request: m.ContextRequest | JsonObject) -> m.ContextPack:
        """A context pack as JSON (a `ContextRequest` or its JSON object)."""
        return self._c.send(ep.context_pack(domain, _context_request(request)))

    def context_markdown(self, domain: str, request: m.ContextRequest | JsonObject) -> str:
        """The same context pack rendered as Markdown."""
        return self._c.send(ep.context_pack_markdown(domain, _context_request(request)))

    def explain(self, domain: str, kind: str, key: str, *, branch: str | None = None) -> m.Explain:
        """Where the values and Links of a Record came from."""
        return self._c.send(ep.explain_record(domain, kind, key, branch=branch))

    def explain_markdown(
        self, domain: str, kind: str, key: str, *, branch: str | None = None
    ) -> str:
        return self._c.send(ep.explain_record_markdown(domain, kind, key, branch=branch))

    def health(self, domain: str, *, branch: str | None = None) -> m.DomainHealth:
        """Violations, orphan Links, gaps and duplicates per Kind and Relation of a Domain."""
        return self._c.send(ep.domain_health(domain, branch=branch))

    def health_markdown(self, domain: str, *, branch: str | None = None) -> str:
        """The Domain health report rendered as Markdown."""
        return self._c.send(ep.domain_health_markdown(domain, branch=branch))

    def kind_health(
        self, domain: str, kind: str, *, branch: str | None = None
    ) -> m.KindHealthReport:
        """The health of one Kind with the Relations it takes part in."""
        return self._c.send(ep.kind_health(domain, kind, branch=branch))

    def kind_health_markdown(self, domain: str, kind: str, *, branch: str | None = None) -> str:
        return self._c.send(ep.kind_health_markdown(domain, kind, branch=branch))

    def changes(
        self,
        domain: str,
        branch: str,
        *,
        since: str | None = None,
        kinds: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> m.ChangesFeed:
        """Revision events of a branch after `since` (a Revision id or tag)."""
        return self._c.send(
            ep.branch_changes(domain, branch, since=since, kinds=kinds, limit=limit)
        )

    def events(
        self,
        domain: str,
        branch: str,
        *,
        kinds: Sequence[str] | None = None,
        last_event_id: str | None = None,
    ) -> str:
        """The event stream of a branch as text, read until the server closes it."""
        return self._c.send(
            ep.branch_events(domain, branch, kinds=kinds, last_event_id=last_event_id)
        )


class Webhooks(_Resource):
    def create(
        self,
        domain: str,
        url: str,
        *,
        events: Sequence[str] | None = None,
        branches: Sequence[str] | None = None,
        description: str | None = None,
    ) -> m.WebhookCreated:
        """Register a webhook; the signing secret is in `secret` and is not shown again."""
        body = m.CreateWebhook(
            url=url,
            events=None if events is None else list(events),
            branches=None if branches is None else list(branches),
            description=description,
        )
        return self._c.send(ep.create_webhook(domain, body))

    def list(self, domain: str) -> m.WebhookList:
        return self._c.send(ep.list_webhooks(domain))

    def delete(self, domain: str, webhook_id: str) -> None:
        self._c.send(ep.delete_webhook(domain, webhook_id))


class Sources(_Resource):
    def sync(self, domain: str, source: str, *, dry_run: bool = False) -> m.SyncQueued:
        """Queue a synchronization of the Source (id or api name) for Scribe."""
        return self._c.send(ep.sync_source(domain, source, m.SyncRequest(dry_run=dry_run)))

    def runs(
        self, domain: str, source: str, *, limit: int | None = None, cursor: str | None = None
    ) -> m.Page[m.SourceRun]:
        """Runs of the Source, newest first (needs a Domain-wide `data.read` grant)."""
        return self._c.send(ep.list_source_runs(domain, source, limit=limit, cursor=cursor))

    def iterate_runs(
        self, domain: str, source: str, *, page_size: int | None = None
    ) -> Iterator[m.SourceRun]:
        return self._c.iterate(ep.list_source_runs(domain, source, limit=page_size))
