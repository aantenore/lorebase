"""What each Herald tool does: Keeper calls with the agent's credential, then a compact view.

The functions here know nothing about MCP; `server.py` registers them as tools with their
schemas and descriptions. Read tools only read Keeper's REST API: no tool calls a model.

`lorebase_context` and `lorebase_explain` use Keeper's agent endpoints (`POST .../context`,
`GET .../records/{key}/explain`). Against a Keeper without them (the route itself is missing)
they fall back to composing the answer from the read endpoints.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from lorebase_herald import views
from lorebase_herald.context_pack import ContextPack
from lorebase_herald.errors import invalid_input, tool_error
from lorebase_herald.keeper_http import (
    KeeperError,
    KeeperHttp,
    KeeperUnavailableError,
    route_missing,
    segment,
)
from lorebase_herald.render import render
from lorebase_herald.settings import HeraldSettings

T = TypeVar("T")


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

    def address(self) -> dict[str, str]:
        return {"kind": self.kind, "key": self.key}


class LorebaseTools:
    """The tool implementations over one Keeper client."""

    def __init__(self, keeper: KeeperHttp, settings: HeraldSettings) -> None:
        self.keeper = keeper
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

    async def call(self, request: Awaitable[T]) -> T:
        """Await a Keeper call, turning failures into tool errors the model can act on."""
        try:
            return await request
        except (KeeperError, KeeperUnavailableError) as error:
            raise tool_error(error, max_issues=self.output.max_issues) from None

    async def newer(self, request: Callable[[], Awaitable[T]]) -> T | None:
        """`request()` to an endpoint older Keepers lack; None when this Keeper lacks it."""
        try:
            return await request()
        except KeeperError as error:
            if route_missing(error):
                return None
            raise tool_error(error, max_issues=self.output.max_issues) from None
        except KeeperUnavailableError as error:
            raise tool_error(error, max_issues=self.output.max_issues) from None

    def path(self, domain: str, *parts: str) -> str:
        return KeeperHttp.domain_path(domain, *parts)

    async def fetch_schema(self, credential: str | None, domain: str, branch: str | None) -> Any:
        return await self.keeper.get(
            self.path(domain, "schema"),
            credential=credential,
            params={"branch": branch, "format": "json"},
        )

    async def fetch_record(
        self,
        credential: str | None,
        domain: str,
        record: RecordRef,
        *,
        branch: str | None,
        at: str | None = None,
        links: bool = False,
        lineage: bool = False,
    ) -> Any:
        return await self.keeper.get(
            self.path(domain, "kinds", segment(record.kind), "records", segment(record.key)),
            credential=credential,
            params={"branch": branch, "at": at, "links": links, "lineage": lineage},
        )

    async def fetch_neighborhood(
        self, credential: str | None, domain: str, body: Mapping[str, Any]
    ) -> Any:
        return await self.keeper.post(
            self.path(domain, "neighborhood"), credential=credential, body=body
        )

    async def fetch_search(
        self, credential: str | None, domain: str, body: Mapping[str, Any]
    ) -> Any:
        return await self.keeper.post(self.path(domain, "search"), credential=credential, body=body)

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
        document = await self.call(self.fetch_schema(credential, name, self.branch(branch)))
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
        body = await self.call(
            self.fetch_record(
                credential,
                self.domain(domain),
                target,
                branch=self.branch(branch),
                at=at,
                links=links and at is None,
            )
        )
        view = views.record_view(body["record"])
        view["revision"] = body.get("revision")
        text = views.record_text(view, body.get("revision"), self.output)
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
        name = self.domain(domain)
        found = await self.call(
            self.keeper.post(self.path(name, "query"), credential=credential, body=body)
        )
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
        fmt: str | None,
    ) -> str:
        if not query and not query_vector:
            raise invalid_input(
                "query-required",
                "give `query` text (lexical search) or a `query_vector`",
                "pass the words to look for in `query`",
            )
        body: dict[str, Any] = {}
        if query:
            body["q"] = query
        if query_vector:
            body["query_vector"] = list(query_vector)
        else:
            body["mode"] = "lexical"
        for name, value in (("kinds", kinds), ("where", where), ("limit", limit)):
            if value:
                body[name] = value
        if branch := self.branch(branch):
            body["branch"] = branch
        found = await self.call(self.fetch_search(credential, self.domain(domain), body))
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
        fmt: str | None,
    ) -> str:
        body: dict[str, Any] = {
            "seeds": [RecordRef.parse(s).address() for s in seeds],
            "depth": depth,
            "max_nodes": max_nodes or self.settings.neighborhood.default_max_nodes,
        }
        if relations:
            body["relations"] = list(relations)
        if max_edges:
            body["max_edges"] = max_edges
        if branch := self.branch(branch):
            body["branch"] = branch
        found = await self.call(self.fetch_neighborhood(credential, self.domain(domain), body))
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
        fmt: str | None,
    ) -> str:
        settings = self.settings.context
        name = self.domain(domain)
        branch = self.branch(branch)
        seeds = [RecordRef.parse(r) for r in records or ()]
        found_by: str | None = None
        if not seeds:
            if not question:
                raise invalid_input(
                    "seeds-required",
                    "give `records` (Kind/key refs) or a `question`",
                    "name the Records the task is about, or describe it in `question`",
                )
            body: dict[str, Any] = {
                "q": question,
                "mode": "lexical",
                "limit": settings.search_seeds,
            }
            if branch:
                body["branch"] = branch
            hits = await self.call(self.fetch_search(credential, name, body))
            seeds = [RecordRef(hit["kind"], hit["key"]) for hit in hits.get("items") or []]
            found_by = "search"
            if not seeds:
                return (
                    f"no Records match {question!r}\n"
                    "next: try other words with lorebase_search, or read lorebase_schema"
                )
        seeds = seeds[: settings.max_seeds]
        budget = min(token_budget or settings.token_budget, settings.max_token_budget)
        as_json = self.fmt(fmt) == "json"
        request: dict[str, Any] = {
            "refs": [f"{s.kind}/{s.key}" for s in seeds],
            "depth": depth or settings.depth,
            "budget_tokens": budget,
            "format": "json" if as_json else "markdown",
        }
        if relations:
            request["relations"] = list(relations)
        if branch:
            request["branch"] = branch
        pack = await self.newer(
            lambda: self.keeper.post_text(
                self.path(name, "context"), credential=credential, body=request
            )
        )
        if pack is None:
            return await self._composed_context(
                credential,
                name,
                branch,
                seeds,
                depth=depth,
                relations=relations,
                budget=budget,
                found_by=found_by,
                question=question,
                as_json=as_json,
            )
        if found_by is None:
            return pack
        if as_json:
            document = json.loads(pack)
            document.update(found_by=found_by, question=question)
            return json.dumps(document, separators=(",", ":"), ensure_ascii=False)
        return f"seeds found by {found_by} for {question!r}\n{pack}"

    async def _composed_context(
        self,
        credential: str | None,
        name: str,
        branch: str | None,
        seeds: Sequence[RecordRef],
        *,
        depth: int | None,
        relations: Sequence[str] | None,
        budget: int,
        found_by: str | None,
        question: str | None,
        as_json: bool,
    ) -> str:
        """A context pack built here from the read endpoints, for Keepers without `/context`."""
        settings = self.settings.context
        hood: dict[str, Any] = {
            "seeds": [s.address() for s in seeds],
            "depth": depth or settings.depth,
            "max_nodes": settings.max_nodes,
            "max_edges": settings.max_edges,
        }
        if relations:
            hood["relations"] = list(relations)
        if branch:
            hood["branch"] = branch
        schema, neighborhood, *seed_bodies = await self.call(
            asyncio.gather(
                self.fetch_schema(credential, name, branch),
                self.fetch_neighborhood(credential, name, hood),
                *(self.fetch_record(credential, name, s, branch=branch, links=True) for s in seeds),
            )
        )
        pack = ContextPack(
            schema=schema,
            neighborhood=neighborhood,
            seeds=[body["record"] for body in seed_bodies],
            found_by=found_by,
            question=question,
            output=self.output,
        )
        max_chars = int(budget * settings.chars_per_token)
        if as_json:
            return pack.json(max_chars)
        return pack.text(max_chars)

    async def explain(
        self,
        credential: str | None,
        *,
        record: str,
        attributes: Sequence[str] | None,
        domain: str | None,
        branch: str | None,
        fmt: str | None,
    ) -> str:
        target = RecordRef.parse(record)
        name, branch = self.domain(domain), self.branch(branch)
        explained = await self.newer(
            lambda: self.keeper.get(
                self.path(
                    name, "kinds", segment(target.kind), "records", segment(target.key), "explain"
                ),
                credential=credential,
                params={"branch": branch, "format": "json"},
            )
        )
        if explained is not None:
            view = views.explain_view(explained, attributes, self.output)
            text = views.explain_text(view, self.output)
            return render(view, text, self.fmt(fmt), self.output)
        body = await self.call(
            self.fetch_record(credential, name, target, branch=branch, lineage=True)
        )
        view = views.lineage_view(body, attributes, self.output)
        return render(view, views.lineage_text(view, self.output), self.fmt(fmt), self.output)

    async def default_branch(self, credential: str | None, domain: str) -> str:
        found = await self.call(self.keeper.get(self.path(domain), credential=credential))
        return str(found["default_branch"])

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
        branch_name = self.branch(branch) or await self.default_branch(credential, name)
        path = self.path(name, "branches", segment(branch_name), "log")
        page_size = min(self.settings.changes.page_size, max(limit, 1))
        items: list[dict[str, Any]] = []
        reached = False
        head: str | None = None
        next_cursor = cursor
        for _ in range(self.settings.changes.max_pages):
            page = await self.call(
                self.keeper.get(
                    path,
                    credential=credential,
                    params={
                        "limit": page_size,
                        "cursor": next_cursor,
                        "author": author,
                        "origin_type": origin_type,
                    },
                )
            )
            head = head or page.get("head")
            next_cursor = page.get("next_cursor")
            for entry in page.get("items") or []:
                if since and _same_revision(entry["id"], since):
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
        body = await self.call(
            self.keeper.get(
                self.path(self.domain(domain), "diff"),
                credential=credential,
                params={
                    "from": from_,
                    "to": to,
                    "kinds": list(kinds) if kinds else None,
                    "relations": list(relations) if relations else None,
                    "limit": limit,
                    "cursor": cursor,
                },
            )
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
        body: dict[str, Any] = {"parameters": dict(parameters), "dry_run": dry_run}
        for key, value in (
            ("branch", self.branch(branch)),
            ("expected_head", expected_head),
            ("idempotency_key", idempotency_key),
            ("message", message),
            ("task", task),
            ("intent", intent),
        ):
            if value:
                body[key] = value
        for key, flag in (("auto_propose", auto_propose), ("auto_merge", auto_merge)):
            if flag is not None:
                body[key] = flag
        path = self.path(self.domain(domain), "operations", segment(operation), "execute")
        found = await self.call(self.keeper.post(path, credential=credential, body=body))
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
        body = await self.call(
            self.keeper.get(
                self.path(self.domain(domain), "branches"),
                credential=credential,
                params={"limit": limit, "cursor": cursor},
            )
        )
        view = views.branches_view(body, self.output)
        return render(view, views.branches_text(view), self.fmt(fmt), self.output)


MIN_REVISION_PREFIX = 8


def _same_revision(revision: str, wanted: str) -> bool:
    """Exact id, or an unambiguous-looking prefix of at least eight hex characters."""
    wanted = wanted.lower()
    return revision == wanted or (
        len(wanted) >= MIN_REVISION_PREFIX and revision.startswith(wanted)
    )
