"""What each Herald tool does: Keeper calls with the agent's credential, then a compact view.

The functions here know nothing about MCP; `server.py` registers them as tools with their
schemas and descriptions. Read tools only read Keeper's REST API through the typed Lorebase
client: no tool calls a model.

`lorebase_context` and `lorebase_explain` use Keeper's agent endpoints (`POST .../context`,
`GET .../records/{key}/explain`); `lorebase_history` and `lorebase_lineage` read
`GET .../records/{key}/history` and `.../lineage`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from lorebase_client import ApiError, AsyncLorebaseClient, LorebaseConnectionError
from lorebase_client import models as m
from lorebase_herald import views
from lorebase_herald.errors import (
    invalid_input,
    invalid_request,
    tool_error,
    unexpected_answer,
)
from lorebase_herald.keeper import KeeperClients
from lorebase_herald.render import as_json, render
from lorebase_herald.settings import HeraldSettings


@dataclass(frozen=True)
class RecordRef:
    kind: str
    key: str

    @classmethod
    def parse(cls, value: str) -> RecordRef:
        kind, _, key = value.partition("/")
        if not kind or not key:
            raise invalid_input(
                "invalid-ref",
                f"{value!r} is not a `Kind/key` ref",
                "write refs as Kind/key, e.g. Supplier/S-1001 (see lorebase_search)",
            )
        return cls(kind, key)

    def address(self) -> m.RecordAddress:
        return m.RecordAddress(kind=self.kind, key=self.key)


class LorebaseTools:
    """The tool implementations over the clients of Keeper."""

    def __init__(self, clients: KeeperClients, settings: HeraldSettings) -> None:
        self.clients = clients
        self.settings = settings
        self.output = settings.output

    # Helpers ---------------------------------------------------------------------------------

    def domain(self, domain: str | None) -> str:
        chosen = domain or self.settings.domain
        if not chosen:
            raise invalid_input(
                "domain-required",
                "no Domain named and none configured",
                "pass `domain` (Domain id), or set LOREBASE_DOMAIN for this server",
            )
        return chosen

    def branch(self, branch: str | None) -> str | None:
        return branch or self.settings.branch

    def fmt(self, fmt: str | None) -> str:
        return fmt or self.output.default_format

    @asynccontextmanager
    async def session(self, credential: str | None) -> AsyncIterator[AsyncLorebaseClient]:
        """The client of one tool call, with its failures turned into tool errors the model can
        act on."""
        try:
            async with self.clients.open(credential) as client:
                yield client
        except (ApiError, LorebaseConnectionError) as error:
            raise tool_error(error, max_issues=self.output.max_issues) from None
        except ValidationError as error:
            raise unexpected_answer(error) from None

    def request[M: BaseModel](self, model: type[M], **members: Any) -> M:
        """A request model, its rejection reported as a tool error before anything is sent."""
        try:
            return model(**members)
        except ValidationError as error:
            raise invalid_request(error, max_issues=self.output.max_issues) from None

    # Tools -----------------------------------------------------------------------------------

    async def schema(
        self,
        credential: str | None,
        *,
        domain: str | None,
        branch: str | None,
        kinds: Sequence[str] | None,
        detail: str,
        fmt: str | None,
    ) -> str:
        name = self.domain(domain)
        async with self.session(credential) as client:
            document = await client.schema.get(name, branch=self.branch(branch))
        view = views.schema_view(document, kinds=kinds, detail=detail)
        text = views.schema_text(view, detail=detail, output=self.output)
        return render(view, text, self.fmt(fmt), self.output)

    async def get(
        self,
        credential: str | None,
        *,
        record: str,
        domain: str | None,
        branch: str | None,
        at: str | None,
        links: bool,
        fmt: str | None,
    ) -> str:
        target = RecordRef.parse(record)
        name = self.domain(domain)
        async with self.session(credential) as client:
            found = await client.records.get(
                name,
                target.kind,
                target.key,
                branch=self.branch(branch),
                at=at,
                links=links and at is None,
            )
        view = views.record_view(found.record)
        view["revision"] = found.revision
        text = views.record_text(view, found.revision, self.output)
        return render(view, text, self.fmt(fmt), self.output)

    async def query(
        self,
        credential: str | None,
        *,
        query: Mapping[str, Any],
        cursor: str | None,
        domain: str | None,
        branch: str | None,
        at: str | None,
        fmt: str | None,
    ) -> str:
        body: dict[str, Any] = {**query}
        body.pop("branch", None)
        body.pop("at", None)
        if cursor:
            body["cursor"] = cursor
        if branch := self.branch(branch):
            body["branch"] = branch
        if at:
            body["at"] = at
        request = self.request(m.QueryRequest, **body)
        name = self.domain(domain)
        async with self.session(credential) as client:
            found = await client.records.query(name, request)
        view = views.page_view(found, self.output)
        return render(view, views.page_text(view, self.output), self.fmt(fmt), self.output)

    async def search(
        self,
        credential: str | None,
        *,
        query: str | None,
        kinds: Sequence[str] | None,
        where: Mapping[str, Any] | None,
        query_vector: Sequence[float] | None,
        limit: int | None,
        domain: str | None,
        branch: str | None,
        at: str | None,
        fmt: str | None,
    ) -> str:
        if not query and not query_vector:
            raise invalid_input(
                "query-required",
                "give `query` text (lexical search) or a `query_vector`",
                "pass the words to look for in `query`",
            )
        request = self.request(
            m.SearchRequest,
            q=query or None,
            query_vector=list(query_vector) if query_vector else None,
            mode=None if query_vector else "lexical",
            kinds=list(kinds) if kinds else None,
            where=dict(where) if where else None,
            limit=limit or None,
            branch=self.branch(branch),
            at=at,
        )
        name = self.domain(domain)
        async with self.session(credential) as client:
            found = await client.records.search(name, request)
        view = views.search_view(found, self.output)
        return render(view, views.search_text(view, self.output), self.fmt(fmt), self.output)

    async def neighborhood(
        self,
        credential: str | None,
        *,
        seeds: Sequence[str],
        depth: int,
        relations: Sequence[str] | None,
        max_nodes: int | None,
        max_edges: int | None,
        values: bool,
        domain: str | None,
        branch: str | None,
        at: str | None,
        fmt: str | None,
    ) -> str:
        request = self.request(
            m.NeighborhoodRequest,
            seeds=[RecordRef.parse(s).address() for s in seeds],
            depth=depth,
            max_nodes=max_nodes or self.settings.neighborhood.default_max_nodes,
            relations=list(relations) if relations else None,
            max_edges=max_edges or None,
            branch=self.branch(branch),
            at=at,
        )
        name = self.domain(domain)
        async with self.session(credential) as client:
            found = await client.records.neighborhood(name, request)
        view = views.neighborhood_view(found, self.output, values=values)
        text = views.neighborhood_text(view, self.output)
        return render(view, text, self.fmt(fmt), self.output)

    async def context(
        self,
        credential: str | None,
        *,
        records: Sequence[str] | None,
        question: str | None,
        depth: int | None,
        relations: Sequence[str] | None,
        token_budget: int | None,
        domain: str | None,
        branch: str | None,
        at: str | None,
        fmt: str | None,
    ) -> str:
        settings = self.settings.context
        name = self.domain(domain)
        branch = self.branch(branch)
        seeds = [RecordRef.parse(r) for r in records or ()]
        if not seeds and not question:
            raise invalid_input(
                "seeds-required",
                "give `records` (Kind/key refs) or a `question`",
                "name the Records the task is about, or describe it in `question`",
            )
        as_json_pack = self.fmt(fmt) == "json"
        async with self.session(credential) as client:
            if not seeds:
                hits = await client.records.search(
                    name,
                    self.request(
                        m.SearchRequest,
                        q=question,
                        mode="lexical",
                        limit=settings.search_seeds,
                        branch=branch,
                        at=at,
                    ),
                )
                seeds = [RecordRef(hit.kind, hit.key) for hit in hits.items]
                if not seeds:
                    return (
                        f"no Records match {question!r}\n"
                        "next: try other words with lorebase_search, or read lorebase_schema"
                    )
                found_by: str | None = "search"
            else:
                found_by = None
            request = self.request(
                m.ContextRequest,
                refs=[f"{s.kind}/{s.key}" for s in seeds[: settings.max_seeds]],
                depth=depth or settings.depth,
                budget_tokens=min(token_budget or settings.token_budget, settings.max_token_budget),
                relations=list(relations) if relations else None,
                branch=branch,
                at=at,
            )
            if as_json_pack:
                pack = await client.agents.context(name, request)
                document = pack.model_dump(mode="json", by_alias=True, exclude_unset=True)
                if found_by is not None:
                    document.update(found_by=found_by, question=question)
                return as_json(document)
            markdown = await client.agents.context_markdown(name, request)
        if found_by is None:
            return markdown
        return f"seeds found by {found_by} for {question!r}\n{markdown}"

    async def explain(
        self,
        credential: str | None,
        *,
        record: str,
        attributes: Sequence[str] | None,
        domain: str | None,
        branch: str | None,
        at: str | None,
        fmt: str | None,
    ) -> str:
        target = RecordRef.parse(record)
        name = self.domain(domain)
        async with self.session(credential) as client:
            explained = await client.agents.explain(
                name, target.kind, target.key, branch=self.branch(branch), at=at
            )
        view = views.explain_view(explained, attributes, self.output)
        text = views.explain_text(view, self.output)
        return render(view, text, self.fmt(fmt), self.output)

    async def history(
        self,
        credential: str | None,
        *,
        record: str,
        values: bool,
        limit: int | None,
        cursor: str | None,
        domain: str | None,
        branch: str | None,
        at: str | None,
        fmt: str | None,
    ) -> str:
        target = RecordRef.parse(record)
        name = self.domain(domain)
        async with self.session(credential) as client:
            found = await client.records.history(
                name,
                target.kind,
                target.key,
                branch=self.branch(branch),
                at=at,
                values=values,
                limit=limit,
                cursor=cursor,
            )
        view = views.history_view(found, self.output, values=values)
        return render(view, views.history_text(view, self.output), self.fmt(fmt), self.output)

    async def lineage(
        self,
        credential: str | None,
        *,
        record: str,
        domain: str | None,
        branch: str | None,
        fmt: str | None,
        at: str | None = None,
    ) -> str:
        target = RecordRef.parse(record)
        name = self.domain(domain)
        async with self.session(credential) as client:
            found = await client.records.lineage(
                name, target.kind, target.key, branch=self.branch(branch), at=at
            )
        view = views.lineage_view(found, self.output)
        return render(view, views.lineage_text(view, self.output), self.fmt(fmt), self.output)

    async def changes(
        self,
        credential: str | None,
        *,
        since: str | None,
        limit: int,
        author: str | None,
        origin_type: str | None,
        cursor: str | None,
        domain: str | None,
        branch: str | None,
        fmt: str | None,
    ) -> str:
        name = self.domain(domain)
        page_size = min(self.settings.changes.page_size, max(limit, 1))
        items: list[dict[str, Any]] = []
        reached = False
        head: str | None = None
        next_cursor = cursor
        async with self.session(credential) as client:
            branch_name = self.branch(branch) or (await client.domains.get(name)).default_branch
            for _ in range(self.settings.changes.max_pages):
                page = await client.revisions.log(
                    name,
                    branch_name,
                    author=author,
                    origin_type=origin_type,
                    limit=page_size,
                    cursor=next_cursor,
                )
                head = head or page.head
                next_cursor = page.next_cursor
                for entry in page.items:
                    if since and _same_revision(entry.id, since):
                        reached = True
                        break
                    items.append(views.log_entry(entry))
                    if len(items) >= limit:
                        break
                if reached or len(items) >= limit or not next_cursor:
                    break
        view: dict[str, Any] = {
            "branch": branch_name,
            "head": head,
            "since": since,
            "since_reached": reached if since else None,
            "items": items,
        }
        if not reached and next_cursor:
            view["next_cursor"] = next_cursor
        return render(view, views.changes_text(view, self.output), self.fmt(fmt), self.output)

    async def diff(
        self,
        credential: str | None,
        *,
        from_: str,
        to: str,
        kinds: Sequence[str] | None,
        relations: Sequence[str] | None,
        limit: int | None,
        cursor: str | None,
        domain: str | None,
        fmt: str | None,
    ) -> str:
        name = self.domain(domain)
        async with self.session(credential) as client:
            body = await client.revisions.diff(
                name, from_, to, kinds=kinds, relations=relations, limit=limit, cursor=cursor
            )
        view = views.diff_view(body, self.output)
        return render(view, views.diff_text(view, self.output), self.fmt(fmt), self.output)

    async def execute(
        self,
        credential: str | None,
        *,
        operation: str,
        parameters: Mapping[str, Any],
        dry_run: bool,
        expected_head: str | None,
        idempotency_key: str | None,
        message: str | None,
        task: str | None,
        intent: str | None,
        auto_propose: bool | None,
        auto_merge: bool | None,
        domain: str | None,
        branch: str | None,
        fmt: str | None,
    ) -> str:
        name = self.domain(domain)
        async with self.session(credential) as client:
            found: m.Execution | m.AgentWrite
            if task or intent or auto_propose is not None or auto_merge is not None:
                found = await client.operations.execute_as_agent(
                    name,
                    operation,
                    parameters,
                    task=task or None,
                    intent=intent or None,
                    auto_propose=auto_propose,
                    auto_merge=auto_merge,
                    branch=self.branch(branch),
                    dry_run=dry_run,
                    expected_head=expected_head or None,
                    message=message or None,
                    idempotency_key=idempotency_key or None,
                )
            else:
                found = await client.operations.execute(
                    name,
                    operation,
                    parameters,
                    branch=self.branch(branch),
                    dry_run=dry_run,
                    expected_head=expected_head or None,
                    message=message or None,
                    idempotency_key=idempotency_key or None,
                )
        view = views.execution_view(found, self.output)
        return render(view, views.execution_text(view, self.output), self.fmt(fmt), self.output)

    async def branches(
        self,
        credential: str | None,
        *,
        limit: int | None,
        cursor: str | None,
        domain: str | None,
        fmt: str | None,
    ) -> str:
        name = self.domain(domain)
        async with self.session(credential) as client:
            body = await client.branches.list(name, limit=limit, cursor=cursor)
        view = views.branches_view(body, self.output)
        return render(view, views.branches_text(view), self.fmt(fmt), self.output)


MIN_REVISION_PREFIX = 8


def _same_revision(revision: str, wanted: str) -> bool:
    """Exact id, or an unambiguous-looking prefix of at least eight hex characters."""
    wanted = wanted.lower()
    return revision == wanted or (
        len(wanted) >= MIN_REVISION_PREFIX and revision.startswith(wanted)
    )
