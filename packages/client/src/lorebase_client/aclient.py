"""Asynchronous client: `AsyncLorebaseClient`.

The same API as `LorebaseClient` (`lorebase_client.client`) with coroutines. List endpoints
have `list` (one page, awaited) and `iterate` (an async iterator over all items).

    async with AsyncLorebaseClient("http://localhost:8080", api_key="lbk_...") as client:
        async for record in client.records.iterate("supply", "Supplier", branch="work"):
            print(record.key, record.title)
"""

from __future__ import annotations

import asyncio
import copy
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from types import TracebackType
from typing import Any, Self

import httpx
from pydantic import JsonValue

from lorebase_client import endpoints as ep
from lorebase_client import models as m
from lorebase_client.config import (
    ClientConfig,
    RetryPolicy,
    TokenSource,
    TraceMode,
    check_session_id,
    resolve_config,
)
from lorebase_client.core import Call, Done, Exchange, RequestCore, Retry, new_idempotency_key

JsonObject = Mapping[str, Any]


class AsyncLorebaseClient:
    """Non-blocking client over `httpx.AsyncClient`; arguments as for `LorebaseClient`.

    `transport` may be an `httpx.ASGITransport` to call an in-process application. `event_hooks`
    holds coroutine functions (`request` / `response`) run on every HTTP request sent.
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
        session_id: str | None = None,
        tool: str | None = None,
        trace: TraceMode | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        trust_env: bool = True,
        event_hooks: Mapping[str, Sequence[Callable[..., Any]]] | None = None,
        env: Mapping[str, str] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
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
            session_id=session_id,
            tool=tool,
            trace=trace,
            env=env,
        )
        self._core = RequestCore(self.config)
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            timeout=self.config.timeout_s,
            transport=transport,
            trust_env=trust_env,
            event_hooks={k: list(v) for k, v in (event_hooks or {}).items()},
        )
        self._mount()

    def _mount(self) -> None:
        self.domains = Domains(self)
        self.branches = Branches(self)
        self.tags = Tags(self)
        self.schema = SchemaApi(self)
        self.operations = Operations(self)
        self.records = Records(self)
        self.revisions = Revisions(self)
        self.api_keys = ApiKeys(self)
        self.vault = Vault(self)
        self.grants = Grants(self)
        self.principals = Principals(self)
        self.audit = Audit(self)
        self.trace = Trace(self)
        self.proposals = Proposals(self)
        self.agents = Agents(self)
        self.webhooks = Webhooks(self)
        self.reconciliation = Reconciliation(self)
        self.sources = Sources(self)
        self.documents = Documents(self)

    def with_session(
        self, session_id: str, *, tool: str | None = None, trace: TraceMode | None = None
    ) -> Self:
        """A client for another agent trace session over the same connection and cache.

        It sends `session_id` (and `tool`, `trace` when given; otherwise this client's) with
        every request. Closing either client closes the shared connection.
        """
        derived = copy.copy(self)
        derived.config = replace(
            self.config,
            session_id=check_session_id(session_id),
            tool=tool or self.config.tool,
            trace=trace or self.config.trace,
        )
        derived._core = RequestCore(derived.config)
        derived._core.cache = self._core.cache
        derived._mount()
        return derived

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # Request layer ---------------------------------------------------------------------

    async def send[T](self, call: Call[T]) -> T:
        """Run a call, retrying as the retry policy says; returns the parsed answer."""
        exchange = Exchange(self._core, call)
        step: Done[T] | Retry
        while True:
            request = self._http.build_request(**exchange.request_args())
            try:
                response = await self._http.send(request)
            except httpx.TransportError as error:
                step = exchange.on_error(error)
            else:
                step = exchange.on_response(response)
            if isinstance(step, Done):
                return step.value
            await self._sleep(step.delay)

    async def pages[P: m.Paged[Any]](self, call: Call[P]) -> AsyncIterator[P]:
        """Every page of a list call, following `next_cursor`."""
        cursor = call.params.get("cursor")
        while True:
            page = await self.send(call.with_params(cursor=cursor))
            yield page
            cursor = page.next_cursor
            if cursor is None:
                return

    async def iterate[T](self, call: Call[m.Paged[T]]) -> AsyncIterator[T]:
        """Every item of a list call."""
        async for page in self.pages(call):
            for item in page.items:
                yield item

    async def whoami(self) -> m.WhoAmI:
        """The authenticated caller."""
        return await self.send(ep.whoami())

    def idempotency_key(self, prefix: str | None = None) -> str:
        """A fresh key to pass as `idempotency_key=` to a write."""
        return new_idempotency_key(prefix)

    def cache_clear(self) -> None:
        """Forget cached answers (no effect when the cache is off)."""
        if self._core.cache is not None:
            self._core.cache.clear()


class _Resource:
    def __init__(self, client: AsyncLorebaseClient) -> None:
        self._c = client


class Domains(_Resource):
    async def create(
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
        return await self._c.send(ep.create_domain(body, idempotency_key=idempotency_key))

    async def list(
        self, *, limit: int | None = None, cursor: str | None = None
    ) -> m.Page[m.Domain]:
        return await self._c.send(ep.list_domains(limit=limit, cursor=cursor))

    def iterate(self, *, page_size: int | None = None) -> AsyncIterator[m.Domain]:
        return self._c.iterate(ep.list_domains(limit=page_size))

    async def get(self, id: str) -> m.Domain:
        return await self._c.send(ep.get_domain(id))

    async def update(
        self, id: str, *, display_name: str | None = None, description: str | None = None
    ) -> m.Domain:
        body = m.DomainUpdate(display_name=display_name, description=description)
        return await self._c.send(ep.update_domain(id, body))

    async def delete(self, id: str, *, confirm: str) -> None:
        """Delete the Domain with everything in it; `confirm` repeats its id."""
        await self._c.send(ep.delete_domain(id, confirm=confirm))


class Branches(_Resource):
    async def list(
        self, domain: str, *, limit: int | None = None, cursor: str | None = None
    ) -> m.Page[m.Branch]:
        return await self._c.send(ep.list_branches(domain, limit=limit, cursor=cursor))

    def iterate(self, domain: str, *, page_size: int | None = None) -> AsyncIterator[m.Branch]:
        return self._c.iterate(ep.list_branches(domain, limit=page_size))

    async def create(
        self,
        domain: str,
        name: str,
        *,
        source: str | None = None,
        revision: str | None = None,
        mode: m.BranchMode | None = None,
        idempotency_key: str | None = None,
    ) -> m.Branch:
        body = m.BranchCreate(name=name, source=source, revision=revision, mode=mode)
        return await self._c.send(ep.create_branch(domain, body, idempotency_key=idempotency_key))

    async def get(self, domain: str, name: str) -> m.BranchDetail:
        return await self._c.send(ep.get_branch(domain, name))

    async def set_protected(self, domain: str, name: str, protected: bool) -> m.Branch:
        return await self._c.send(ep.protect_branch(domain, name, protected=protected))

    async def delete(self, domain: str, name: str) -> None:
        await self._c.send(ep.delete_branch(domain, name))

    async def rebase_tags(
        self,
        domain: str,
        name: str,
        *,
        message: str | None = None,
        expected_head: str | None = None,
        dry_run: bool = False,
        rows_only: bool = False,
    ) -> m.RebaseTagsResult:
        """Commit the default branch's sensitivity tags to the branch's schema (or, with
        `rows_only`, queue the rewrite of its stale titles and search vectors)."""
        body = m.RebaseTagsRequest(
            message=message, expected_head=expected_head, dry_run=dry_run, rows_only=rows_only
        )
        return await self._c.send(ep.rebase_branch_tags(domain, name, body))


class Tags(_Resource):
    async def list(
        self, domain: str, *, limit: int | None = None, cursor: str | None = None
    ) -> m.Page[m.Tag]:
        return await self._c.send(ep.list_tags(domain, limit=limit, cursor=cursor))

    def iterate(self, domain: str, *, page_size: int | None = None) -> AsyncIterator[m.Tag]:
        return self._c.iterate(ep.list_tags(domain, limit=page_size))

    async def create(
        self,
        domain: str,
        name: str,
        *,
        source: str | None = None,
        revision: str | None = None,
        idempotency_key: str | None = None,
    ) -> m.Tag:
        body = m.TagCreate(name=name, source=source, revision=revision)
        return await self._c.send(ep.create_tag(domain, body, idempotency_key=idempotency_key))


class SchemaApi(_Resource):
    async def get(
        self, domain: str, *, branch: str | None = None, at: str | None = None
    ) -> m.SchemaDocument:
        return await self._c.send(ep.get_schema(domain, branch=branch, at=at))

    async def get_yaml(
        self, domain: str, *, branch: str | None = None, at: str | None = None
    ) -> str:
        return await self._c.send(ep.get_schema_yaml(domain, branch=branch, at=at))

    async def element(
        self,
        domain: str,
        section: m.SchemaSection,
        name: str,
        *,
        branch: str | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        return await self._c.send(
            ep.get_schema_element(domain, section, name, branch=branch, at=at)
        )

    async def validate(
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
        return await self._c.send(ep.validate_schema(domain, body))

    async def apply(
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
        return await self._c.send(ep.apply_schema(domain, branch, body))

    async def infer_postgres(
        self,
        domain: str,
        dsn: str,
        *,
        schema: str | None = None,
        tables: Sequence[str] | None = None,
        options: JsonObject | None = None,
    ) -> dict[str, Any]:
        """Propose a schema from Postgres tables (`dsn` may be a `secret://` reference)."""
        return await self._c.send(
            ep.infer_schema_postgres(domain, _infer_body(dsn, schema, tables, options))
        )

    async def infer_files(
        self,
        domain: str,
        files: Mapping[str, bytes],
        *,
        options: JsonObject | None = None,
        records_path: str | None = None,
    ) -> dict[str, Any]:
        """Propose a schema from data files (file name to content); `records_path` (dotted keys)
        locates the records array of JSON documents that hold it under a key."""
        return await self._c.send(
            ep.infer_schema_files(domain, files, options=options, records_path=records_path)
        )

    async def preview_files(
        self,
        domain: str,
        files: Mapping[str, bytes],
        *,
        rows: int | None = None,
        records_path: str | None = None,
    ) -> dict[str, Any]:
        """The first rows of each data file as Keeper reads them; nothing is stored."""
        return await self._c.send(
            ep.preview_files(domain, files, rows=rows, records_path=records_path)
        )

    async def preview_derivation(self, domain: str, body: JsonObject) -> dict[str, Any]:
        """Evaluate a candidate Derivation on sample Records without saving it."""
        return await self._c.send(ep.preview_derivation(domain, body))

    async def preview_source(self, domain: str, body: JsonObject) -> dict[str, Any]:
        """Evaluate the expression mappings of a candidate Source on sample rows."""
        return await self._c.send(ep.preview_source(domain, body))

    async def suggestions(
        self,
        domain: str,
        *,
        status: Sequence[m.ItemDecisionValue] | None = None,
        type: Sequence[m.SuggestionType] | None = None,
        all: bool | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> m.Page[m.SchemaSuggestion]:
        """One page of schema suggestions from extraction, the most supported first."""
        return await self._c.send(
            ep.list_schema_suggestions(
                domain, status=status, type=type, all=all, limit=limit, cursor=cursor
            )
        )

    def iterate_suggestions(
        self,
        domain: str,
        *,
        status: Sequence[m.ItemDecisionValue] | None = None,
        type: Sequence[m.SuggestionType] | None = None,
        all: bool | None = None,
        page_size: int | None = None,
    ) -> AsyncIterator[m.SchemaSuggestion]:
        call = ep.list_schema_suggestions(
            domain, status=status, type=type, all=all, limit=page_size
        )
        return self._c.iterate(call)

    async def decide_suggestions(
        self, domain: str, decisions: Sequence[m.SuggestionDecision | JsonObject]
    ) -> m.SuggestionsDecided:
        """Reject, reset or accept suggestions; the accepted ones become one schema change
        set on a new branch, proposed into the default branch (`proposal`)."""
        body = m.DecideSuggestions(
            decisions=[
                d if isinstance(d, m.SuggestionDecision) else m.SuggestionDecision(**d)
                for d in decisions
            ]
        )
        return await self._c.send(ep.decide_schema_suggestions(domain, body))

    async def merge_suggestions(self, domain: str) -> m.SuggestionsMerged:
        """Fold the stored Attribute suggestions that are variants of one another."""
        return await self._c.send(ep.merge_schema_suggestions(domain))


class Operations(_Resource):
    async def execute(
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
        return await self._c.send(
            ep.execute_operation(domain, operation, body, idempotency_key=idempotency_key)
        )

    async def execute_as_agent(
        self,
        domain: str,
        operation: str,
        parameters: JsonObject | None = None,
        *,
        task: str | None = None,
        intent: str | None = None,
        auto_propose: bool | None = None,
        auto_merge: bool | None = None,
        branch: str | None = None,
        dry_run: bool = False,
        expected_head: str | None = None,
        message: str | None = None,
        idempotency_key: str | None = None,
    ) -> m.Execution | m.AgentWrite:
        """Execute an Operation that may need review. When it does (protected branch, review
        policy) it runs on the branch `agent/{principal}/{task}` and the answer is an
        `AgentWrite` naming the Proposal; otherwise it is the usual `Execution`. Without a `task`
        Keeper refuses `auto_propose` and answers 409 `review-required` when review is needed."""
        body = m.ExecuteRequest(
            parameters=dict(parameters or {}),
            branch=branch,
            dry_run=dry_run,
            expected_head=expected_head,
            message=message,
            task=task,
            intent=intent,
            auto_propose=auto_propose,
            auto_merge=auto_merge,
        )
        return await self._c.send(
            ep.execute_operation_as_agent(domain, operation, body, idempotency_key=idempotency_key)
        )

    async def import_records(
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
        return await self.execute(
            domain,
            "import_records",
            {"kind": kind, "rows": list(rows)},
            branch=branch,
            dry_run=dry_run,
            expected_head=expected_head,
            message=message,
            idempotency_key=idempotency_key,
        )

    async def import_links(
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
        return await self.execute(
            domain,
            "import_links",
            {"relation": relation, "rows": list(rows)},
            branch=branch,
            dry_run=dry_run,
            expected_head=expected_head,
            message=message,
            idempotency_key=idempotency_key,
        )

    async def import_batch(
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
        return await self.execute(
            domain,
            "import_batch",
            {"records": list(records), "links": list(links)},
            branch=branch,
            dry_run=dry_run,
            expected_head=expected_head,
            message=message,
            idempotency_key=idempotency_key,
        )

    async def delete_records(
        self,
        domain: str,
        kind: str,
        keys: Sequence[str],
        *,
        skip_missing: bool = False,
        branch: str | None = None,
        dry_run: bool = False,
        expected_head: str | None = None,
        message: str | None = None,
        idempotency_key: str | None = None,
    ) -> m.Execution:
        """Delete Records of `kind` by key in one Revision; Links follow each Relation's
        `on_delete`. Unknown keys fail the call unless `skip_missing` (then warnings)."""
        parameters: dict[str, Any] = {"kind": kind, "keys": list(keys)}
        if skip_missing:
            parameters["skip_missing"] = True
        return await self.execute(
            domain,
            "delete_records",
            parameters,
            branch=branch,
            dry_run=dry_run,
            expected_head=expected_head,
            message=message,
            idempotency_key=idempotency_key,
        )


class Records(_Resource):
    async def list(
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
        return await self._c.send(
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
    ) -> AsyncIterator[m.RecordItem]:
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

    async def get(
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
        return await self._c.send(
            ep.get_record(domain, kind, key, branch=branch, at=at, links=links, lineage=lineage)
        )

    async def history(
        self,
        domain: str,
        kind: str,
        key: str,
        *,
        branch: str | None = None,
        at: str | None = None,
        values: bool = False,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> m.RecordHistory:
        """One page of the changes of a Record, newest first."""
        return await self._c.send(
            ep.record_history(
                domain, kind, key, branch=branch, at=at, values=values, limit=limit, cursor=cursor
            )
        )

    def iterate_history(
        self,
        domain: str,
        kind: str,
        key: str,
        *,
        branch: str | None = None,
        at: str | None = None,
        values: bool = False,
        page_size: int | None = None,
    ) -> AsyncIterator[m.RecordChange]:
        call = ep.record_history(
            domain, kind, key, branch=branch, at=at, values=values, limit=page_size
        )
        return self._c.iterate(call)

    async def lineage(
        self,
        domain: str,
        kind: str,
        key: str,
        *,
        branch: str | None = None,
        at: str | None = None,
    ) -> m.RecordLineage:
        """Where the values of a Record came from, at the branch head or at `at`."""
        return await self._c.send(ep.record_lineage(domain, kind, key, branch=branch, at=at))

    async def links(
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
        return await self._c.send(
            ep.list_record_links(
                domain, kind, key, relation, branch=branch, at=at, limit=limit, cursor=cursor
            )
        )

    async def relation_links(
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
        return await self._c.send(
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
    ) -> AsyncIterator[m.LinkItem]:
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

    async def query(self, domain: str, query: m.QueryRequest | JsonObject) -> m.QueryResult:
        """Run a DSL query (a `QueryRequest` or the JSON object of the DSL)."""
        return await self._c.send(ep.query(domain, _query_request(query)))

    async def iterate_query(
        self, domain: str, query: m.QueryRequest | JsonObject
    ) -> AsyncIterator[m.RecordItem]:
        """All Records a query matches, page after page."""
        request = _query_request(query)
        while True:
            page = await self._c.send(ep.query(domain, request))
            for item in page.items:
                yield item
            if page.next_cursor is None:
                return
            request = request.model_copy(update={"cursor": page.next_cursor})

    async def traverse(
        self, domain: str, request: m.TraverseRequest | JsonObject
    ) -> m.TraverseResult:
        body = request if isinstance(request, m.TraverseRequest) else m.TraverseRequest(**request)
        return await self._c.send(ep.traverse(domain, body))

    async def neighborhood(
        self, domain: str, request: m.NeighborhoodRequest | JsonObject
    ) -> m.NeighborhoodResult:
        body = (
            request
            if isinstance(request, m.NeighborhoodRequest)
            else m.NeighborhoodRequest(**request)
        )
        return await self._c.send(ep.neighborhood(domain, body))

    async def search(self, domain: str, request: m.SearchRequest | JsonObject) -> m.SearchResult:
        body = request if isinstance(request, m.SearchRequest) else m.SearchRequest(**request)
        return await self._c.send(ep.search(domain, body))


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
    async def log(
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
        return await self._c.send(
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
    ) -> AsyncIterator[m.Revision]:
        call = ep.branch_log(
            domain,
            branch,
            all_parents=all_parents,
            origin_type=origin_type,
            author=author,
            limit=page_size,
        )
        return self._c.iterate(call)

    async def get(self, domain: str, revision: str) -> m.Revision:
        """A Revision by id, tag or branch name."""
        return await self._c.send(ep.get_revision(domain, revision))

    async def counts(self, domain: str, revision: str) -> m.RevisionCounts:
        return await self._c.send(ep.revision_counts(domain, revision))

    async def changeset(
        self,
        domain: str,
        revision: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> m.ChangesPage:
        return await self._c.send(
            ep.revision_changeset(domain, revision, limit=limit, cursor=cursor)
        )

    def iterate_changeset(
        self, domain: str, revision: str, *, page_size: int | None = None
    ) -> AsyncIterator[m.Change]:
        return self._c.iterate(ep.revision_changeset(domain, revision, limit=page_size))

    async def diff(
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
        return await self._c.send(
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
    ) -> AsyncIterator[m.Change]:
        call = ep.diff(
            domain, from_, to, kinds=kinds, relations=relations, schema=schema, limit=page_size
        )
        return self._c.iterate(call)


class Vault(_Resource):
    """Erasure of personal data held in the vault (`pii.erase`) and key rotation
    (`vault.manage`, installation administrators)."""

    async def keys(self) -> m.VaultKeys:
        """Rows by encryption key and fingerprint key, and the earlier keys no row uses."""
        return await self._c.send(ep.vault_keys())

    async def rotate(self) -> m.VaultRotation:
        """Queue the job that makes rows stored under earlier keys again."""
        return await self._c.send(ep.rotate_vault())

    async def erase_record(
        self,
        domain: str,
        kind: str,
        key: str,
        *,
        attributes: Sequence[str] | None = None,
        branch: str | None = None,
    ) -> m.EraseResult:
        """Erase every value the Record ever held in its vault Attributes (or `attributes`)."""
        body = m.EraseRequest(
            kind=kind,
            key=key,
            attributes=list(attributes) if attributes else None,
            branch=branch,
        )
        return await self._c.send(ep.erase_vault(domain, body))

    async def erase_value(
        self,
        domain: str,
        kind: str,
        attribute: str,
        value: JsonValue,
        *,
        branch: str | None = None,
    ) -> m.EraseResult:
        """Erase `value` of a vault Attribute in every Record of the Kind."""
        body = m.EraseRequest(kind=kind, attribute=attribute, value=value, branch=branch)
        return await self._c.send(ep.erase_vault(domain, body))


class ApiKeys(_Resource):
    async def create(
        self,
        domain: str,
        name: str,
        *,
        role: str | None = None,
        ttl_days: int | None = None,
        principal_id: str | None = None,
        allowed_networks: Sequence[str] | None = None,
    ) -> m.ApiKeyCreated:
        """Create a key; the secret is in `key` of the answer and is not shown again.

        `allowed_networks` (CIDR) limits the client addresses the key works from.
        """
        body = m.CreateApiKey(
            name=name,
            role=role,
            ttl_days=ttl_days,
            principal_id=principal_id,
            allowed_networks=list(allowed_networks) if allowed_networks else None,
        )
        return await self._c.send(ep.create_api_key(domain, body))

    async def list(
        self, domain: str, *, limit: int | None = None, cursor: str | None = None
    ) -> m.Page[m.ApiKey]:
        return await self._c.send(ep.list_api_keys(domain, limit=limit, cursor=cursor))

    def iterate(self, domain: str, *, page_size: int | None = None) -> AsyncIterator[m.ApiKey]:
        return self._c.iterate(ep.list_api_keys(domain, limit=page_size))

    async def revoke(self, domain: str, key_id: str) -> None:
        await self._c.send(ep.revoke_api_key(domain, key_id))


class Grants(_Resource):
    async def create(
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
        return await self._c.send(ep.create_grant(domain, body))

    async def list(
        self, domain: str, *, limit: int | None = None, cursor: str | None = None
    ) -> m.Page[m.Grant]:
        return await self._c.send(ep.list_grants(domain, limit=limit, cursor=cursor))

    def iterate(self, domain: str, *, page_size: int | None = None) -> AsyncIterator[m.Grant]:
        return self._c.iterate(ep.list_grants(domain, limit=page_size))

    async def revoke(self, domain: str, grant_id: str) -> None:
        await self._c.send(ep.revoke_grant(domain, grant_id))


class Principals(_Resource):
    async def list(
        self, *, type_: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> m.Page[m.Principal]:
        return await self._c.send(ep.list_principals(type_=type_, limit=limit, cursor=cursor))

    def iterate(
        self, *, type_: str | None = None, page_size: int | None = None
    ) -> AsyncIterator[m.Principal]:
        return self._c.iterate(ep.list_principals(type_=type_, limit=page_size))


class Audit(_Resource):
    async def list(
        self, domain: str, *, limit: int | None = None, cursor: str | None = None
    ) -> m.Page[m.AuditEntry]:
        return await self._c.send(ep.list_audit(domain, limit=limit, cursor=cursor))

    def iterate(self, domain: str, *, page_size: int | None = None) -> AsyncIterator[m.AuditEntry]:
        return self._c.iterate(ep.list_audit(domain, limit=page_size))

    async def verify(self, domain: str, *, after_seq: int | None = None) -> m.AuditVerification:
        """Recompute the Domain's audit hash chain; `intact` and `first_broken` tell the result."""
        return await self._c.send(ep.verify_audit(domain, after_seq=after_seq))


class Trace(_Resource):
    """Agent trace: sessions the caller may see, their timelines and notes."""

    async def sessions(
        self,
        *,
        actor: str | None = None,
        domain_id: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> m.TraceSessionPage:
        """One page of sessions, most recent first; `enabled` tells whether Keeper traces."""
        call = ep.list_trace_sessions(actor=actor, domain_id=domain_id, limit=limit, cursor=cursor)
        return await self._c.send(call)

    def iterate(
        self,
        *,
        actor: str | None = None,
        domain_id: str | None = None,
        page_size: int | None = None,
    ) -> AsyncIterator[m.TraceSession]:
        return self._c.iterate(
            ep.list_trace_sessions(actor=actor, domain_id=domain_id, limit=page_size)
        )

    async def session(self, session_id: str, *, actor: str | None = None) -> m.TraceTimeline:
        """The calls and notes of a session in order."""
        return await self._c.send(ep.get_trace_session(session_id, actor=actor))

    async def add_note(
        self,
        session_id: str,
        text: str,
        *,
        domain_id: str | None = None,
        step: int | None = None,
    ) -> m.NoteAccepted:
        """Attach a note to the caller's session; it is written off the request path."""
        body = m.NoteRequest(text=text, domain_id=domain_id, step=step)
        return await self._c.send(ep.add_trace_note(session_id, body))


class Proposals(_Resource):
    async def create(
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
        return await self._c.send(ep.create_proposal(domain, body, idempotency_key=idempotency_key))

    async def list(
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
        return await self._c.send(
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
    ) -> AsyncIterator[m.Proposal]:
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

    async def get(self, domain: str, proposal_id: str) -> m.ProposalDetail:
        return await self._c.send(ep.get_proposal(domain, proposal_id))

    async def update(
        self,
        domain: str,
        proposal_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> m.ProposalDetail:
        body = m.ProposalUpdate(title=title, description=description)
        return await self._c.send(ep.update_proposal(domain, proposal_id, body))

    async def close(self, domain: str, proposal_id: str) -> m.ProposalDetail:
        return await self._c.send(ep.close_proposal(domain, proposal_id))

    async def review(
        self,
        domain: str,
        proposal_id: str,
        verdict: m.ReviewVerdict,
        *,
        comment: str | None = None,
    ) -> m.ReviewResult:
        body = m.ReviewIn(verdict=verdict, comment=comment)
        return await self._c.send(ep.review_proposal(domain, proposal_id, body))

    async def merge(
        self,
        domain: str,
        proposal_id: str,
        *,
        message: str | None = None,
        idempotency_key: str | None = None,
    ) -> m.MergeResult:
        body = m.MergeIn(message=message)
        return await self._c.send(
            ep.merge_proposal(domain, proposal_id, body, idempotency_key=idempotency_key)
        )

    async def conflicts(
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
        return await self._c.send(
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
    ) -> AsyncIterator[m.ConflictItem]:
        call = ep.list_conflicts(domain, proposal_id, kind=kind, status=status, limit=page_size)
        return self._c.iterate(call)

    async def resolve(
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
        return await self._c.send(ep.resolve_conflict(domain, proposal_id, conflict_id, body))

    async def resolve_many(
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
        return await self._c.send(ep.resolve_conflicts(domain, proposal_id, body))

    async def items(
        self,
        domain: str,
        proposal_id: str,
        *,
        status: Sequence[m.ItemDecisionValue] | None = None,
        kind: Sequence[str] | None = None,
        type: Sequence[m.ItemType] | None = None,
        min_confidence: float | None = None,
        max_confidence: float | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> m.Page[m.ProposalItem]:
        """One page of the items of an extraction Proposal; a filter given several values
        matches any of them."""
        return await self._c.send(
            ep.list_proposal_items(
                domain,
                proposal_id,
                status=status,
                kind=kind,
                type=type,
                min_confidence=min_confidence,
                max_confidence=max_confidence,
                limit=limit,
                cursor=cursor,
            )
        )

    def iterate_items(
        self,
        domain: str,
        proposal_id: str,
        *,
        status: Sequence[m.ItemDecisionValue] | None = None,
        kind: Sequence[str] | None = None,
        type: Sequence[m.ItemType] | None = None,
        min_confidence: float | None = None,
        max_confidence: float | None = None,
        page_size: int | None = None,
    ) -> AsyncIterator[m.ProposalItem]:
        call = ep.list_proposal_items(
            domain,
            proposal_id,
            status=status,
            kind=kind,
            type=type,
            min_confidence=min_confidence,
            max_confidence=max_confidence,
            limit=page_size,
        )
        return self._c.iterate(call)

    async def decide_items(
        self,
        domain: str,
        proposal_id: str,
        decisions: Sequence[m.ItemDecision | JsonObject] = (),
        *,
        where: m.ItemSelection | JsonObject | None = None,
        decision: m.ItemDecisionValue | None = None,
    ) -> m.ItemsDecided:
        """Record decisions by item, and or one `decision` for every item matching `where`;
        the extraction branch and the Proposal are refreshed."""
        body = m.DecideItems(
            decisions=[
                d if isinstance(d, m.ItemDecision) else m.ItemDecision(**d) for d in decisions
            ],
            where=where
            if where is None or isinstance(where, m.ItemSelection)
            else m.ItemSelection(**where),
            decision=decision,
        )
        return await self._c.send(ep.decide_proposal_items(domain, proposal_id, body))


class Agents(_Resource):
    async def context(self, domain: str, request: m.ContextRequest | JsonObject) -> m.ContextPack:
        """A context pack as JSON (a `ContextRequest` or its JSON object)."""
        return await self._c.send(ep.context_pack(domain, _context_request(request)))

    async def context_markdown(self, domain: str, request: m.ContextRequest | JsonObject) -> str:
        """The same context pack rendered as Markdown."""
        return await self._c.send(ep.context_pack_markdown(domain, _context_request(request)))

    async def explain(
        self, domain: str, kind: str, key: str, *, branch: str | None = None, at: str | None = None
    ) -> m.Explain:
        """Where the values and Links of a Record came from."""
        return await self._c.send(ep.explain_record(domain, kind, key, branch=branch, at=at))

    async def explain_markdown(
        self, domain: str, kind: str, key: str, *, branch: str | None = None, at: str | None = None
    ) -> str:
        return await self._c.send(
            ep.explain_record_markdown(domain, kind, key, branch=branch, at=at)
        )

    async def health(
        self, domain: str, *, branch: str | None = None, at: str | None = None
    ) -> m.DomainHealth:
        """Violations, orphan Links, gaps and duplicates per Kind and Relation of a Domain."""
        return await self._c.send(ep.domain_health(domain, branch=branch, at=at))

    async def health_markdown(
        self, domain: str, *, branch: str | None = None, at: str | None = None
    ) -> str:
        """The Domain health report rendered as Markdown."""
        return await self._c.send(ep.domain_health_markdown(domain, branch=branch, at=at))

    async def kind_health(
        self, domain: str, kind: str, *, branch: str | None = None, at: str | None = None
    ) -> m.KindHealthReport:
        """The health of one Kind with the Relations it takes part in."""
        return await self._c.send(ep.kind_health(domain, kind, branch=branch, at=at))

    async def kind_health_markdown(
        self, domain: str, kind: str, *, branch: str | None = None, at: str | None = None
    ) -> str:
        return await self._c.send(ep.kind_health_markdown(domain, kind, branch=branch, at=at))

    async def changes(
        self,
        domain: str,
        branch: str,
        *,
        since: str | None = None,
        kinds: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> m.ChangesFeed:
        """Revision events of a branch after `since` (a Revision id or tag)."""
        return await self._c.send(
            ep.branch_changes(domain, branch, since=since, kinds=kinds, limit=limit)
        )

    async def events(
        self,
        domain: str,
        branch: str,
        *,
        kinds: Sequence[str] | None = None,
        last_event_id: str | None = None,
    ) -> str:
        """The event stream of a branch as text, read until the server closes it."""
        return await self._c.send(
            ep.branch_events(domain, branch, kinds=kinds, last_event_id=last_event_id)
        )


class Webhooks(_Resource):
    async def create(
        self,
        domain: str,
        url: str,
        *,
        events: Sequence[str] | None = None,
        branches: Sequence[str] | None = None,
        description: str | None = None,
        clearances: Sequence[str] | None = None,
    ) -> m.WebhookCreated:
        """Register a webhook; the signing secret is in `secret` and is not shown again."""
        body = m.CreateWebhook(
            url=url,
            events=None if events is None else list(events),
            branches=None if branches is None else list(branches),
            description=description,
            clearances=None if clearances is None else list(clearances),
        )
        return await self._c.send(ep.create_webhook(domain, body))

    async def list(self, domain: str) -> m.WebhookList:
        return await self._c.send(ep.list_webhooks(domain))

    async def set_clearances(
        self, domain: str, webhook_id: str, clearances: Sequence[str]
    ) -> m.Webhook:
        """Replace the clearances of a webhook; adding one needs its clearance permission on
        the Domain."""
        body = m.UpdateWebhook(clearances=list(clearances))
        return await self._c.send(ep.update_webhook(domain, webhook_id, body))

    async def delete(self, domain: str, webhook_id: str) -> None:
        await self._c.send(ep.delete_webhook(domain, webhook_id))


class Reconciliation(_Resource):
    async def list(
        self,
        domain: str,
        *,
        branch: str | None = None,
        type: m.CandidateType | None = None,
        status: m.CandidateStatus | None = None,
        kind: str | None = None,
        attribute: str | None = None,
        source: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> m.Page[m.ReconciliationCandidate]:
        """One page of the candidates of a branch (default: the default branch), newest first.

        `kind` and `attribute` are api names (`attribute` needs `kind`); `source` is a Source
        identity such as `source:src_...`."""
        return await self._c.send(
            ep.list_reconciliation(
                domain,
                branch=branch,
                type=type,
                status=status,
                kind=kind,
                attribute=attribute,
                source=source,
                limit=limit,
                cursor=cursor,
            )
        )

    def iterate(
        self,
        domain: str,
        *,
        branch: str | None = None,
        type: m.CandidateType | None = None,
        status: m.CandidateStatus | None = None,
        kind: str | None = None,
        attribute: str | None = None,
        source: str | None = None,
        page_size: int | None = None,
    ) -> AsyncIterator[m.ReconciliationCandidate]:
        call = ep.list_reconciliation(
            domain,
            branch=branch,
            type=type,
            status=status,
            kind=kind,
            attribute=attribute,
            source=source,
            limit=page_size,
        )
        return self._c.iterate(call)

    async def get(self, domain: str, candidate_id: str) -> m.ReconciliationCandidate:
        return await self._c.send(ep.get_reconciliation_candidate(domain, candidate_id))

    async def resolve(
        self,
        domain: str,
        candidate_id: str,
        choice: m.ReconcileChoice,
        *,
        reason: str,
        claim_id: str | None = None,
        value: JsonValue = None,
        policy: m.ReconcilePolicy | None = None,
        survivor: str | None = None,
        values: JsonObject | None = None,
        dry_run: bool = False,
    ) -> m.ReconciliationDecision:
        """Decide a candidate; `dry_run` reports the outcome without writing."""
        body = m.ReconcileIn(
            choice=choice,
            claim_id=claim_id,
            value=value,
            policy=policy,
            survivor=survivor,
            values=None if values is None else dict(values),
            reason=reason,
            dry_run=dry_run,
        )
        return await self._c.send(ep.resolve_reconciliation(domain, candidate_id, body))

    async def dismiss(
        self, domain: str, candidate_id: str, *, reason: str
    ) -> m.ReconciliationDecision:
        return await self._c.send(
            ep.dismiss_reconciliation(domain, candidate_id, m.ReasonIn(reason=reason))
        )

    async def reopen(
        self, domain: str, candidate_id: str, *, reason: str
    ) -> m.ReconciliationDecision:
        """Reopen a dismissed candidate."""
        return await self._c.send(
            ep.reopen_reconciliation(domain, candidate_id, m.ReasonIn(reason=reason))
        )

    async def resolve_many(
        self,
        domain: str,
        choice: m.BulkChoice,
        *,
        reason: str,
        ids: Sequence[str] | None = None,
        where: m.ReconcileFilter | None = None,
        policy: m.ReconcilePolicy | None = None,
        dry_run: bool = False,
    ) -> m.BulkDecision:
        """Apply a policy or a dismissal to the `ids` given, or to the open candidates
        matching `where`."""
        body = m.BulkReconcileIn(
            ids=None if ids is None else list(ids),
            filter=where,
            choice=choice,
            policy=policy,
            reason=reason,
            dry_run=dry_run,
        )
        return await self._c.send(ep.resolve_reconciliation_bulk(domain, body))

    async def queue_sweep(self, domain: str, *, branch: str | None = None) -> m.SweepQueued:
        """Queue a reconciliation sweep of a branch for Scribe."""
        return await self._c.send(
            ep.queue_reconciliation_sweep(domain, m.SweepRequest(branch=branch))
        )


class Sources(_Resource):
    async def sync(self, domain: str, source: str, *, dry_run: bool = False) -> m.SyncQueued:
        """Queue a synchronization of the Source (id or api name) for Scribe."""
        return await self._c.send(ep.sync_source(domain, source, m.SyncRequest(dry_run=dry_run)))

    async def runs(
        self, domain: str, source: str, *, limit: int | None = None, cursor: str | None = None
    ) -> m.Page[m.SourceRun]:
        """Runs of the Source, newest first (needs a Domain-wide `data.read` grant)."""
        return await self._c.send(ep.list_source_runs(domain, source, limit=limit, cursor=cursor))

    def iterate_runs(
        self, domain: str, source: str, *, page_size: int | None = None
    ) -> AsyncIterator[m.SourceRun]:
        return self._c.iterate(ep.list_source_runs(domain, source, limit=page_size))


class Documents(_Resource):
    async def upload(
        self,
        domain: str,
        file: str | os.PathLike[str] | bytes,
        *,
        filename: str | None = None,
        extract: bool | None = None,
        tiers: Sequence[int] | None = None,
        source_key: str | None = None,
    ) -> m.DocumentUploaded:
        """Upload a document: a path (named after the file) or bytes with a `filename`.

        `extract` (server default: true) queues an extraction job on the given `tiers`. With a
        `source_key`, new bytes become a new version of the key's document.
        """
        call = ep.upload_document(
            domain, file, filename=filename, extract=extract, tiers=tiers, source_key=source_key
        )
        return await self._c.send(call)

    async def upload_url(
        self,
        domain: str,
        url: str,
        *,
        filename: str | None = None,
        extract: bool | None = None,
        tiers: Sequence[int] | None = None,
        source_key: str | None = None,
    ) -> m.DocumentUploaded:
        """Have the server fetch the document from an allowlisted URL."""
        body = m.DocumentFromUrl(
            url=url,
            filename=filename,
            extract=extract,
            tiers=None if tiers is None else list(tiers),
            source_key=source_key,
        )
        return await self._c.send(ep.upload_document_url(domain, body))

    async def list(
        self,
        domain: str,
        *,
        source_key: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> m.Page[m.Document]:
        """Documents of the Domain, newest first; `source_key` keeps one key's versions."""
        call = ep.list_documents(domain, source_key=source_key, limit=limit, cursor=cursor)
        return await self._c.send(call)

    def iterate(
        self, domain: str, *, source_key: str | None = None, page_size: int | None = None
    ) -> AsyncIterator[m.Document]:
        return self._c.iterate(ep.list_documents(domain, source_key=source_key, limit=page_size))

    async def versions(
        self,
        domain: str,
        document_id: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> m.Page[m.Document]:
        """The versions of the document's source key, newest first."""
        call = ep.document_versions(domain, document_id, limit=limit, cursor=cursor)
        return await self._c.send(call)

    def iterate_versions(
        self, domain: str, document_id: str, *, page_size: int | None = None
    ) -> AsyncIterator[m.Document]:
        return self._c.iterate(ep.document_versions(domain, document_id, limit=page_size))

    async def retire(self, domain: str, document_id: str, *, reason: str) -> m.DocumentRetired:
        """Retire a document: it is never extracted again and its evidence is re-checked."""
        body = m.RetirementIn(reason=reason)
        return await self._c.send(ep.retire_document(domain, document_id, body))

    async def purge(
        self, domain: str, document_id: str, *, reason: str, confirm: str
    ) -> m.DocumentPurged:
        """Irreversibly delete the content of a document; `confirm` is the document id."""
        body = m.PurgeIn(reason=reason, confirm=confirm)
        return await self._c.send(ep.purge_document(domain, document_id, body))

    async def get(self, domain: str, document_id: str) -> m.DocumentDetail:
        """A document with its extraction jobs."""
        return await self._c.send(ep.get_document(domain, document_id))

    async def text(self, domain: str, document_id: str) -> m.DocumentText:
        """The parsed text of a document (404 until it is parsed)."""
        return await self._c.send(ep.get_document_text(domain, document_id))
