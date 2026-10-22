"""The Herald MCP server: tools, resources and the guide prompt (design 11.2).

Tool schemas come from the annotated signatures below; bounds that depend on configuration
(depth, caps, budgets) are read from the `herald` settings when the server is built, so the
schemas a host sees match what Herald accepts.

Credentials: on stdio, every call uses the API key configured for this agent's server process;
on Streamable HTTP, every call forwards the caller's own `Authorization` header and fails
without one. Herald holds no key of its own and never substitutes one: each call opens its own
client with that one credential.
"""

# No `from __future__ import annotations`: tool signatures are evaluated where they are defined
# so that the SDK can read bounds taken from settings.

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from lorebase_herald import __version__, prompts
from lorebase_herald.errors import invalid_input
from lorebase_herald.keeper import KeeperClients
from lorebase_herald.settings import HeraldSettings
from lorebase_herald.tools import LorebaseTools
from lorebase_herald.tracing import Tracing, request_header

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)

DomainArg = Annotated[
    str | None, Field(description="Domain id. Default: the Domain configured for this server.")
]
BranchArg = Annotated[
    str | None, Field(description="Branch name. Default: the configured or default branch.")
]
AtArg = Annotated[str | None, Field(description="Revision id or tag to read at (time travel).")]
FormatArg = Annotated[
    Literal["text", "json"] | None,
    Field(description="text: compact lines (default). json: stable machine-readable shape."),
]
RefArg = Annotated[
    str, Field(min_length=3, description="Record ref `Kind/key`, e.g. Supplier/S-1001.")
]
NamesArg = Annotated[list[str] | None, Field(description="Api names.")]
CursorArg = Annotated[
    str | None, Field(description="`next_cursor` from the previous page of this call.")
]


class Credentials:
    """The credential each Keeper call carries: always the calling agent's own.

    Calling it for a tool also sets the trace headers (session, tool, traceparent) of the
    Keeper calls that tool makes.
    """

    def __init__(self, settings: HeraldSettings) -> None:
        self._settings = settings
        self._tracing = Tracing(settings.trace)

    def __call__(self, ctx: Context | None, tool: str) -> str | None:
        self._tracing.begin(ctx, tool)
        if self._settings.transport == "streamable_http":
            header = self._settings.http.credential_header
            try:
                headers = ctx.headers if ctx is not None else None
            except ValueError:
                headers = None
            value = request_header(headers, header)
            if value is None:
                raise invalid_input(
                    "unauthorized",
                    "this request carries no credential",
                    f"configure the MCP host to send `{header}: Bearer <agent api key>`",
                )
            return value
        key = self._settings.keeper.api_key
        return key.get_secret_value() if key is not None else None


def create_server(settings: HeraldSettings, clients: KeeperClients) -> MCPServer:
    """The MCP server over `clients`, with every tool, resource and prompt registered."""
    server: MCPServer = MCPServer(
        settings.server_name,
        title="Lorebase",
        instructions=prompts.INSTRUCTIONS,
        version=__version__,
        log_level=settings.log_level,
    )
    tools = LorebaseTools(clients, settings)
    credential = Credentials(settings)
    hood = settings.neighborhood
    pack = settings.context
    output = settings.output

    @server.tool(
        name="lorebase_schema",
        description="Kinds (attributes with types; `!` = required), Relations and Operations "
        "of a Domain at a branch. Read this before writing queries or calling Operations.",
        annotations=READ,
        structured_output=False,
    )
    async def lorebase_schema(
        ctx: Context,
        kinds: Annotated[
            list[str] | None,
            Field(description="Only these Kinds (api names) and their Relations."),
        ] = None,
        detail: Annotated[
            Literal["summary", "full"],
            Field(description="summary: types only. full: adds every description."),
        ] = "summary",
        domain: DomainArg = None,
        branch: BranchArg = None,
        format: FormatArg = None,
    ) -> str:
        return await tools.schema(
            credential(ctx, "lorebase_schema"),
            domain=domain,
            branch=branch,
            kinds=kinds,
            detail=detail,
            fmt=format,
        )

    @server.tool(
        name="lorebase_get",
        description="One Record by `Kind/key`: its values and how many Links it has per "
        "Relation. Use lorebase_neighborhood to see the linked Records.",
        annotations=READ,
        structured_output=False,
    )
    async def lorebase_get(
        ctx: Context,
        ref: RefArg,
        links: Annotated[bool, Field(description="Add Link counts per Relation.")] = True,
        at: AtArg = None,
        domain: DomainArg = None,
        branch: BranchArg = None,
        format: FormatArg = None,
    ) -> str:
        return await tools.get(
            credential(ctx, "lorebase_get"),
            record=ref,
            domain=domain,
            branch=branch,
            at=at,
            links=links,
            fmt=format,
        )

    @server.tool(
        name="lorebase_query",
        description="Filter, sort and page Records with the JSON query DSL. Shape: {kind | "
        'trait, where, select, include, order_by, limit}. where: {"attr": name, "op": eq|ne|'
        'lt|lte|gt|gte|in|between|contains|prefix|exists|match, "value": v}, combined with '
        '{"and": [...]}, {"or": [...]}, {"not": p}; {"via": relation, "where": p} filters on '
        'linked Records; {"derived": name, ...} on derived values. include: {relation: '
        '{"select": [...], "limit": n}}. order_by: [{"attr": name, "dir": "asc"|"desc"}].',
        annotations=READ,
        structured_output=False,
    )
    async def lorebase_query(
        ctx: Context,
        query: Annotated[
            dict[str, Any],
            Field(
                description='DSL query, e.g. {"kind": "Part", "where": {"attr": "category", '
                '"op": "eq", "value": "bearing"}, "limit": 20}.'
            ),
        ],
        cursor: CursorArg = None,
        at: AtArg = None,
        domain: DomainArg = None,
        branch: BranchArg = None,
        format: FormatArg = None,
    ) -> str:
        return await tools.query(
            credential(ctx, "lorebase_query"),
            query=query,
            cursor=cursor,
            domain=domain,
            branch=branch,
            at=at,
            fmt=format,
        )

    @server.tool(
        name="lorebase_search",
        description="Find Records by words (lexical full-text over searchable attributes). "
        "Semantic or hybrid search runs only when you pass `query_vector`, an embedding of the "
        "Domain's embedding model (name and dimension: the SEARCH line of lorebase_schema); "
        "Herald never computes embeddings.",
        annotations=READ,
        structured_output=False,
    )
    async def lorebase_search(
        ctx: Context,
        query: Annotated[
            str | None, Field(description="Words to look for (web search syntax).")
        ] = None,
        kinds: NamesArg = None,
        where: Annotated[
            dict[str, Any] | None,
            Field(description="DSL predicate; needs exactly one Kind in `kinds`."),
        ] = None,
        query_vector: Annotated[
            list[float] | None, Field(description="Query embedding for semantic search.")
        ] = None,
        limit: Annotated[
            int | None, Field(ge=1, le=output.max_items, description="Hits returned.")
        ] = None,
        at: AtArg = None,
        domain: DomainArg = None,
        branch: BranchArg = None,
        format: FormatArg = None,
    ) -> str:
        return await tools.search(
            credential(ctx, "lorebase_search"),
            query=query,
            kinds=kinds,
            where=where,
            query_vector=query_vector,
            limit=limit,
            at=at,
            domain=domain,
            branch=branch,
            fmt=format,
        )

    @server.tool(
        name="lorebase_neighborhood",
        description="Records linked to seed Records, breadth first up to `depth` hops, with "
        "the Links between them. Values are left out unless `values` is true: Keeper then "
        "returns only Record headers and Link endpoints.",
        annotations=READ,
        structured_output=False,
    )
    async def lorebase_neighborhood(
        ctx: Context,
        seeds: Annotated[
            list[str],
            Field(min_length=1, max_length=hood.max_seeds, description="`Kind/key` refs."),
        ],
        depth: Annotated[
            int, Field(ge=1, le=hood.max_depth, description="Hops from the seeds.")
        ] = hood.default_depth,
        relations: Annotated[
            list[str] | None,
            Field(
                description="Relation api names to follow (forward name: forward; inverse "
                "name: backward). Default: all."
            ),
        ] = None,
        max_nodes: Annotated[int | None, Field(ge=1, le=hood.max_nodes)] = None,
        max_edges: Annotated[int | None, Field(ge=1, le=hood.max_edges)] = None,
        values: Annotated[
            bool, Field(description="Include the values of each node and Link.")
        ] = False,
        at: AtArg = None,
        domain: DomainArg = None,
        branch: BranchArg = None,
        format: FormatArg = None,
    ) -> str:
        return await tools.neighborhood(
            credential(ctx, "lorebase_neighborhood"),
            seeds=seeds,
            depth=depth,
            relations=relations,
            max_nodes=max_nodes,
            max_edges=max_edges,
            values=values,
            at=at,
            domain=domain,
            branch=branch,
            fmt=format,
        )

    @server.tool(
        name="lorebase_context",
        description="Start here. A context pack for a task: the seed Records (given, or found "
        "by searching `question`), their Links and neighbors, the schema of the Kinds "
        "involved, recent changes and lineage, trimmed to `token_budget`.",
        annotations=READ,
        structured_output=False,
    )
    async def lorebase_context(
        ctx: Context,
        records: Annotated[
            list[str] | None,
            Field(max_length=pack.max_seeds, description="`Kind/key` refs the task is about."),
        ] = None,
        question: Annotated[
            str | None, Field(description="Used to find seeds when `records` is empty.")
        ] = None,
        depth: Annotated[int | None, Field(ge=1, le=hood.max_depth)] = None,
        relations: NamesArg = None,
        token_budget: Annotated[
            int | None,
            Field(
                ge=200,
                le=pack.max_token_budget,
                description=f"Approximate size of the pack. Default {pack.token_budget}.",
            ),
        ] = None,
        at: AtArg = None,
        domain: DomainArg = None,
        branch: BranchArg = None,
        format: FormatArg = None,
    ) -> str:
        return await tools.context(
            credential(ctx, "lorebase_context"),
            records=records,
            question=question,
            depth=depth,
            relations=relations,
            token_budget=token_budget,
            at=at,
            domain=domain,
            branch=branch,
            fmt=format,
        )

    @server.tool(
        name="lorebase_explain",
        description="Why a Record is what it is: how it was created, and for each attribute "
        "the origin (import, operation, extraction, ...), actor, time and Revision of its last "
        "change; without `attributes`, also its Links and the Proposals that changed it.",
        annotations=READ,
        structured_output=False,
    )
    async def lorebase_explain(
        ctx: Context,
        ref: RefArg,
        attributes: Annotated[
            list[str] | None, Field(description="Only these attributes. Default: all.")
        ] = None,
        at: AtArg = None,
        domain: DomainArg = None,
        branch: BranchArg = None,
        format: FormatArg = None,
    ) -> str:
        return await tools.explain(
            credential(ctx, "lorebase_explain"),
            record=ref,
            attributes=attributes,
            at=at,
            domain=domain,
            branch=branch,
            fmt=format,
        )

    @server.tool(
        name="lorebase_history",
        description="The changes of one Record, newest first: Revision, time, author, origin and "
        "the attributes each change touched; `values` adds the value before and after. One page: "
        "pass `next_cursor` as `cursor` for older changes.",
        annotations=READ,
        structured_output=False,
    )
    async def lorebase_history(
        ctx: Context,
        ref: RefArg,
        values: Annotated[bool, Field(description="Include the value before and after.")] = False,
        limit: Annotated[int, Field(ge=1, le=output.max_items)] = min(20, output.max_items),
        cursor: CursorArg = None,
        at: AtArg = None,
        domain: DomainArg = None,
        branch: BranchArg = None,
        format: FormatArg = None,
    ) -> str:
        return await tools.history(
            credential(ctx, "lorebase_history"),
            record=ref,
            values=values,
            limit=limit,
            cursor=cursor,
            at=at,
            domain=domain,
            branch=branch,
            fmt=format,
        )

    @server.tool(
        name="lorebase_lineage",
        description="Where the values of a Record came from: for each attribute its "
        "value, origin, actor, Revision, and the Source, operation, extraction evidence "
        "(document and span) or Proposal behind it. `at` reads them at a past Revision.",
        annotations=READ,
        structured_output=False,
    )
    async def lorebase_lineage(
        ctx: Context,
        ref: RefArg,
        at: AtArg = None,
        domain: DomainArg = None,
        branch: BranchArg = None,
        format: FormatArg = None,
    ) -> str:
        return await tools.lineage(
            credential(ctx, "lorebase_lineage"),
            record=ref,
            domain=domain,
            branch=branch,
            at=at,
            fmt=format,
        )

    @server.tool(
        name="lorebase_changes",
        description="Revisions committed on a branch, newest first, back to (not including) "
        "`since`. Use it to catch up after a Revision you saw earlier; lorebase_diff shows "
        "what changed.",
        annotations=READ,
        structured_output=False,
    )
    async def lorebase_changes(
        ctx: Context,
        since: Annotated[
            str | None,
            Field(description="Revision id (or 8+ char prefix) to stop at. Default: latest."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=output.max_items)] = min(20, output.max_items),
        author: Annotated[str | None, Field(description="Only by this principal.")] = None,
        origin_type: Annotated[
            str | None, Field(description="Only of this origin, e.g. operation, import.")
        ] = None,
        cursor: CursorArg = None,
        domain: DomainArg = None,
        branch: BranchArg = None,
        format: FormatArg = None,
    ) -> str:
        return await tools.changes(
            credential(ctx, "lorebase_changes"),
            since=since,
            limit=limit,
            author=author,
            origin_type=origin_type,
            cursor=cursor,
            domain=domain,
            branch=branch,
            fmt=format,
        )

    @server.tool(
        name="lorebase_diff",
        description="What changed between two Revisions (ids, tags or branch names): counts per "
        "Kind and Relation, then each added (+), modified (~) or removed (-) element with "
        "old -> new values.",
        annotations=READ,
        structured_output=False,
    )
    async def lorebase_diff(
        ctx: Context,
        from_revision: Annotated[str, Field(min_length=1, description="Older side.")],
        to_revision: Annotated[str, Field(min_length=1, description="Newer side.")],
        kinds: NamesArg = None,
        relations: NamesArg = None,
        limit: Annotated[int | None, Field(ge=1, le=output.max_items)] = None,
        cursor: CursorArg = None,
        domain: DomainArg = None,
        format: FormatArg = None,
    ) -> str:
        return await tools.diff(
            credential(ctx, "lorebase_diff"),
            from_=from_revision,
            to=to_revision,
            kinds=kinds,
            relations=relations,
            limit=limit,
            cursor=cursor,
            domain=domain,
            fmt=format,
        )

    @server.tool(
        name="lorebase_execute",
        description="The only way to write: run a governed Operation (listed by "
        "lorebase_schema). Keeper checks permissions, preconditions and validation. dry_run "
        "defaults to true and writes nothing: check edits and violations, then commit with "
        "dry_run=false, expected_head and idempotency_key. Operations that need review "
        "(review policy, protected branch): pass task (short slug; required for the proposal "
        "flow) and intent; Keeper runs them on branch agent/{you}/{task} and answers with the "
        "Proposal id and status and next steps. Without a task Keeper refuses (auto_propose "
        "included) and its problem text is returned. auto_merge only if the Domain allows it.",
        annotations=WRITE,
        structured_output=False,
    )
    async def lorebase_execute(
        ctx: Context,
        operation: Annotated[
            str, Field(min_length=1, description="Operation api name, e.g. create_supplier.")
        ],
        parameters: Annotated[
            dict[str, Any] | None,
            Field(description="Parameters by name; record refs as keys of the target Kind."),
        ] = None,
        dry_run: Annotated[
            bool, Field(description="Compute edits and violations without committing.")
        ] = True,
        expected_head: Annotated[
            str | None,
            Field(description="Fail with stale-head unless the branch head is this Revision."),
        ] = None,
        idempotency_key: Annotated[
            str | None, Field(description="Retries with the same key commit once.")
        ] = None,
        message: Annotated[
            str | None, Field(max_length=2000, description="Revision message.")
        ] = None,
        task: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=200,
                description="Short slug naming the piece of work. Needed to get a Proposal "
                "when the Operation needs review (required for auto_propose); Keeper uses the "
                "branch agent/{you}/{task}.",
            ),
        ] = None,
        intent: Annotated[
            str | None,
            Field(max_length=4000, description="What you are after; goes into the Proposal."),
        ] = None,
        auto_propose: Annotated[
            bool | None,
            Field(description="Open a Proposal when review is needed (default: Keeper's)."),
        ] = None,
        auto_merge: Annotated[
            bool | None,
            Field(description="Merge the Proposal at once, if the Domain allows it for you."),
        ] = None,
        domain: DomainArg = None,
        branch: BranchArg = None,
        format: FormatArg = None,
    ) -> str:
        return await tools.execute(
            credential(ctx, "lorebase_execute"),
            operation=operation,
            parameters=parameters or {},
            dry_run=dry_run,
            expected_head=expected_head,
            idempotency_key=idempotency_key,
            message=message,
            task=task,
            intent=intent,
            auto_propose=auto_propose,
            auto_merge=auto_merge,
            domain=domain,
            branch=branch,
            fmt=format,
        )

    @server.tool(
        name="lorebase_branches",
        description="Branches of a Domain with their head Revision; the default and protected "
        "branches are marked.",
        annotations=READ,
        structured_output=False,
    )
    async def lorebase_branches(
        ctx: Context,
        limit: Annotated[int | None, Field(ge=1, le=output.max_items)] = None,
        cursor: CursorArg = None,
        domain: DomainArg = None,
        format: FormatArg = None,
    ) -> str:
        return await tools.branches(
            credential(ctx, "lorebase_branches"),
            limit=limit,
            cursor=cursor,
            domain=domain,
            fmt=format,
        )

    @server.resource(
        "lorebase://{domain}/schema",
        name="lorebase_schema",
        description="Schema summary of a Domain at its default branch.",
        mime_type="text/plain",
    )
    async def schema_resource(domain: str, ctx: Context) -> str:
        try:
            return await tools.schema(
                credential(ctx, "lorebase_schema"),
                domain=domain,
                branch=None,
                kinds=None,
                detail="summary",
                fmt="text",
            )
        except ToolError as error:
            raise ResourceError(str(error)) from None

    @server.resource(
        "lorebase://{domain}/{kind}/{key}",
        name="lorebase_record",
        description="One Record with its Link counts.",
        mime_type="text/plain",
    )
    async def record_resource(domain: str, kind: str, key: str, ctx: Context) -> str:
        try:
            return await tools.get(
                credential(ctx, "lorebase_record"),
                record=f"{kind}/{key}",
                domain=domain,
                branch=None,
                at=None,
                links=True,
                fmt="text",
            )
        except ToolError as error:
            raise ResourceError(str(error)) from None

    @server.prompt(
        name="lorebase_guide",
        title="How to use Lorebase",
        description="Workflow for working with a Lorebase Domain: context first, targeted "
        "reads, writes through Operations.",
    )
    def lorebase_guide(domain: str | None = None, task: str | None = None) -> str:
        return prompts.guide(domain or settings.domain, task)

    return server


def transport_security(settings: HeraldSettings) -> TransportSecuritySettings | None:
    """DNS rebinding protection for the configured hosts; None keeps the SDK default."""
    http = settings.http
    if not http.allowed_hosts:
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(http.allowed_hosts),
        allowed_origins=list(http.allowed_origins),
    )


async def serve(settings: HeraldSettings, clients: KeeperClients) -> None:
    """Run the configured transport until it stops."""
    server = create_server(settings, clients)
    if settings.transport == "stdio":
        await server.run_stdio_async()
        return
    http = settings.http
    await server.run_streamable_http_async(
        host=http.host,
        port=http.port,
        streamable_http_path=http.path,
        json_response=http.json_response,
        stateless_http=http.stateless,
        transport_security=transport_security(settings),
    )
