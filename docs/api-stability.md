# API stability policy

What clients of Lorebase can rely on between releases: the REST API under `/api/v1`, the Python
client and the command line, the MCP tools of Herald, and the schema document format
`lorebase.schema/v1`. It says which changes a release may make without warning, which ones count
as breaking, and how a breaking change is announced and carried out.

## Status before 1.0

Lorebase is pre-1.0 and follows semantic versioning ([release notes style](launch/changelog-style.md)).
Until 1.0, a minor release (`0.x`) may make a breaking change to any surface on this page. When
it does, the [changelog](../CHANGELOG.md) names it under **Breaking changes**, in the first lines
of the release section, with what to do. Patch releases (`0.x.y`) make no breaking change except
a security fix that cannot be made otherwise.

Everything else on this page is the policy that applies in full from 1.0, and that pre-1.0
releases already follow wherever they can: additive changes, deprecation before removal, and the
compatibility rules below.

## What the policy covers

| Surface | Stable contract | Not covered |
|---------|-----------------|-------------|
| REST API | Every route under `/api/v1` in the committed [OpenAPI document](openapi.json): paths, methods, query parameters, request and response members and their types, success status codes, headers listed below | Routes outside `/api/v1` (the health checks `/healthz` and `/readyz` keep their paths and status codes, not their bodies), the interactive docs page (`/api/v1/docs`, off in production profiles), the order of members in a JSON object |
| Errors | RFC 9457 problem details: the `status`, the machine-readable `code`, the `type` URI built from it (`keeper.problem_type_base` plus the code, `packages/keeper/src/lorebase_keeper/errors.py`), and documented extension members such as `permission` on a 403 and `errors` on a 422 | The wording of `title` and `detail` |
| Permissions | Permission names (`schema.read`, `audit.read`, ...) and the built-in role names, which appear in 403 responses and in grants (`packages/keeper/src/lorebase_keeper/auth/permissions.py`) | Which built-in role holds a permission may widen; it does not narrow without a breaking change note |
| Headers | `ETag` and `If-None-Match`, `Lorebase-Revision`, `Idempotency-Key` and `Idempotent-Replayed`, `Retry-After` on 429 and 503, `Last-Event-ID` on the event stream | Other response headers |
| Pagination | `items` and `next_cursor` (`packages/keeper/src/lorebase_keeper/pagination.py`); a cursor is opaque and valid for the same request only | The content of a cursor |
| Change feed and webhooks | The event document (`type`, `revision.committed` and its members), the SSE framing, the delivery headers `Lorebase-Event`, `Lorebase-Delivery` and `Lorebase-Signature: t=...,v1=...` ([agents](agents.md#webhooks)) | Delivery timing and retry intervals, which are settings |
| Python client | The public names exported by `lorebase_client` and the resource methods of `LorebaseClient` and `AsyncLorebaseClient` | Modules and names starting with `_` |
| Command line | Command names, options, exit codes (`lorebase --help` lists them) and the JSON printed with `--json`, which follows the API's response shapes | Table and text output, progress notes on standard error |
| MCP (Herald) | Tool names, input members, the `json` output format, error codes, resource URIs ([MCP server](mcp.md)) | The `text` output format and the prompt template, both written for models |
| Schema document | `format: lorebase.schema/v1` and the keys it defines ([design 5](design.md#5-domain-model)) | Comments and key order, which an exported document does not keep |

Configuration keys, metric names ([metrics](metrics.md)), the database schema and bundle files are
not API, but a release that changes them says so in the first lines of its notes, like any
breaking change ([release notes style](launch/changelog-style.md)). Database changes are
applied by migrations; bundles carry a format version that import checks.

## Compatible and breaking changes

A release may make these changes at any time; clients must tolerate them:

- new routes, query parameters, optional request members and headers;
- new members in responses, problem details and event documents;
- new values of an enumeration in a response (a problem `code`, a Conflict code, a status, an
  event `type`): treat an unknown value as "other", not as an error;
- new problem codes for conditions that were not reported before;
- a limit raised, or a validation rule relaxed so that a request that failed now succeeds;
- new MCP tools and optional tool inputs, new CLI commands and options;
- new optional keys and sections of the `lorebase.schema/v1` document.

These are breaking, and need the process below (from 1.0, a new major version):

- removing or renaming a route, a parameter, a member, a tool or a command;
- changing the type, format or meaning of a member, or the unit of a value;
- making an optional request member required, or rejecting input that was accepted, except to
  fix a security problem;
- changing the success status code of a route, or the problem `code` of an existing failure;
- requiring a different permission for a route, except to fix a security problem;
- changing a default so that the same request does something different.

A security fix may make a breaking change in any release. Its notes say so under **Security**
and **Breaking changes**, with the advisory.

## Versioning

**Path.** The major API version is the path prefix, `/api/v1` (`keeper.api_prefix`,
`packages/keeper/src/lorebase_keeper/settings.py`). A breaking change after 1.0 goes to
`/api/v2`, which Keeper serves next to `/api/v1` for at least the deprecation period below; there
is no version negotiation by header.

**OpenAPI document.** `info.version` is the version of the Keeper release that generated the
document (`version=__version__` in `packages/keeper/src/lorebase_keeper/app.py`), not the API
version, so it changes with every release while `/api/v1` stays. `docs/openapi.json` is committed:
`make openapi` regenerates it, and `tests/tooling/test_openapi_export.py` fails when it differs
from the application, so every API change shows as a diff of that file in the pull request that
makes it. The Python client is checked against it (`packages/client/tests/test_contract.py`:
response models must have exactly the documented members, request models no undocumented one,
and every documented operation must have an endpoint builder), and the web app's TypeScript
types are generated from it (`pnpm types` in `apps/web`). To see what changed between two
releases, diff `docs/openapi.json` between their tags.

## Deprecation process

Nothing in `/api/v1` is deprecated today, and Keeper does not emit `Deprecation` or `Sunset`
headers yet: no route has needed them. When a route, member, tool or command is deprecated:

1. The replacement ships first, in the same or an earlier release.
2. The OpenAPI document marks the operation or member `deprecated: true`, and the description
   names the replacement. An MCP tool's description and a CLI command's help say "deprecated"
   and name the replacement.
3. Responses of a deprecated route carry `Deprecation` (RFC 9745) with the date it was
   deprecated, `Sunset` (RFC 8594) with the earliest removal date once one is set, and
   `Link: <...>; rel="deprecation"` pointing at the changelog entry.
4. The changelog lists the deprecation under **Changed**, with the replacement and the earliest
   removal release.
5. Removal is a breaking change, listed under **Breaking changes** of the release that makes it.

From 1.0, a deprecated part of `/api/v1` stays for at least six months and at least one minor
release before removal, and removal happens only in a new major version (`/api/v2`). Before 1.0,
a deprecation lasts at least one minor release where the change allows it.

## Client compatibility

- **Same minor.** The Python client, the CLI, Herald and the web app are released together with
  Keeper and share its version. Each package pins the other Lorebase packages to its minor
  version (`lorebase-core>=0.1,<0.2`, [release](release.md#1-version-and-notes)). The supported
  pairing is a client and a Keeper of the same minor version.
- **Newer Keeper, older client.** Works for additive changes: response models ignore unknown
  members (`Model` in `packages/client/src/lorebase_client/models.py`), so new response members
  do not break an older client.
- **Older Keeper, newer client.** Request models reject unknown members before sending
  (`RequestModel` in the same file), so a client never sends a member its own version does not
  know; a member that a newer client knows and an older Keeper does not is refused by Keeper with
  422. Upgrade Keeper first.
- **Herald.** Herald calls Keeper through the Python client. When Keeper's answer does not fit
  Herald's models, the tool error is `keeper-response-invalid` with "use matching Herald and
  Keeper versions" (`packages/herald/src/lorebase_herald/errors.py`). Herald reports its version
  to MCP hosts at initialization (`packages/herald/src/lorebase_herald/server.py`).
- **Other clients.** Generate from the committed OpenAPI document, ignore unknown members and
  enumeration values, follow `next_cursor` instead of building cursors, and branch on the problem
  `code`, never on `detail`.

## MCP tool stability

Herald's tools (`lorebase_context`, `lorebase_schema`, `lorebase_get`, `lorebase_query`,
`lorebase_search`, `lorebase_neighborhood`, `lorebase_explain`, `lorebase_history`,
`lorebase_lineage`, `lorebase_changes`, `lorebase_diff`, `lorebase_execute`,
`lorebase_branches`) follow the rules of the REST API: names and input members are stable, new
tools and optional inputs are additive, and removing or renaming one is breaking. The `json`
output format has stable shapes. The `text` format is written for models and may change in any
release, as may tool descriptions and the `lorebase_guide` prompt. Error codes in `[code]` are
Keeper's problem codes plus Herald's own (`keeper-unavailable`, `keeper-response-invalid`), and
follow the problem code rules. Read tools stay annotated read-only, and `lorebase_execute` stays
the only tool that writes; adding a writing tool is announced as a change of the agent boundary
in the [threat model](threat-model.md).

## Schema document format

A schema document declares `format: lorebase.schema/v1`. Keeper reads a document without
`format` as v1, refuses any other value (`unsupported format, expected lorebase.schema/v1`), and
refuses unknown sections and keys, all as validation problems
(`packages/core/src/lorebase_core/model/yaml_schema.py`: `SCHEMA_FORMAT` and `read`). Exports
always write `format: lorebase.schema/v1`.

Within v1:

- new optional keys and sections may be added in any release. Because unknown keys are refused,
  a document that uses a key added in a later release fails on an older Keeper with a validation
  problem naming the key, instead of losing it silently;
- new values may be added to enumerations (attribute types, merge policies, Source types);
- element ids stay stable, so renames keep history;
- a key is not removed or given another meaning within v1. A key that is deprecated keeps
  working until v2, and the changelog names its replacement.

A change that v1 documents cannot express compatibly needs `lorebase.schema/v2`. When it comes,
Keeper will accept v1 and v2 documents for at least the whole major version in which v2 appears,
so a repository of v1 schema files keeps applying while it is converted.
