# Lorebase web

The web application of Lorebase: Explorer, Canvas, Modeler and Review over the Keeper REST API (docs/design.md, sections 11 and 12). It uses only the public REST API. It runs without a backend in mock mode, and against a real Keeper in live mode (see [Live mode](#live-mode)); the end-to-end tests in `e2e/` drive the built app against Keeper from a compose stack or from the repository.

Stack: React 19, TypeScript (strict), Vite, Tailwind CSS 4, TanStack Router (file-based) and Query, Sigma.js with graphology, CodeMirror 6 and `yaml` for the schema editor, Vitest with Testing Library, Playwright for the end-to-end tests, `openapi-typescript` for the API types.

## Run

Requirements: Node.js 24 and pnpm.

```bash
cd apps/web
pnpm install
cp .env.example .env     # mock mode by default
pnpm dev                 # http://localhost:5173
```

| Script                    | What it does                                   |
| ------------------------- | ---------------------------------------------- |
| `pnpm dev`                | Vite dev server with HMR                       |
| `pnpm build`              | Type-check, then production build into `dist/` |
| `pnpm preview`            | Serve the production build locally             |
| `pnpm test`               | Unit and component tests (Vitest, jsdom)       |
| `pnpm typecheck`          | `tsc --noEmit`                                 |
| `pnpm lint`               | ESLint (type-aware) and Prettier check         |
| `pnpm format`             | Prettier write                                 |
| `pnpm types`              | Regenerate `src/api/generated/keeper.ts`       |
| `pnpm e2e`                | Playwright flows against a real Keeper         |
| `pnpm screenshots`        | Regenerate the PNGs in `docs/images/` (mock)   |
| `pnpm screenshots flow`   | Record `docs/images/tour.gif` (mock)           |
| `pnpm screenshots --live` | The same PNGs from a running Keeper            |

While iterating, run only the tests that cover what you changed, for example `pnpm test src/features/review`.

## Screenshots

`pnpm screenshots` regenerates the pictures used by `docs/tour.md` and the root README. It builds the app in mock mode into `node_modules/.cache/screenshots-dist`, serves it with `vite preview` on a free port, opens each route in Chromium (1440x900, light theme, plus one dark Explorer shot) and writes `docs/images/<screen>.png`. It does not need a backend or a `.env` file.

```bash
pnpm exec playwright install chromium   # once per machine
pnpm screenshots
```

`pnpm screenshots flow` records a scripted walk (Explorer, a Record, Canvas, the Review resolver) with Playwright video and encodes `docs/images/tour.gif` with ffmpeg; without ffmpeg it stitches PNG frames with Pillow through `uv run --with pillow`. Keep the file under 2 MB; the script warns when it is larger. Frame rate, width and palette are in `FLOW.gif`.

`pnpm screenshots --live --keeper http://127.0.0.1:8080` takes the same pictures from the app in live mode (`--keeper` falls back to `LOREBASE_E2E_URL`). The script loads the supply sample under a Domain of its own through the REST API (the loader of the end-to-end tests, `e2e/supply.ts`), prepares a Proposal with two Conflicts, builds the app with `VITE_API_MODE=live` and a same-origin proxy to Keeper, and drops the Domain afterwards (`--keep` leaves it). Keeper must run with authentication off and `keeper.proposals.allow_self_approval: true`, as the end-to-end stack does. The pictures in `docs/images/` are the live ones; without `--live` the mock produces them as before. `tour.gif` is recorded in mock mode only.

The routes, viewport, theme and the browser flags live in the `MOCK_SHOTS`, `LIVE_SHOTS` and `CONFIG` constants at the top of `scripts/screenshots.ts`. The Canvas needs WebGL 2, so Chromium is started with SwiftShader flags; if a graph does not draw, the script says so and the list fallback is captured instead.

## Configuration

Everything comes from Vite environment variables. No URL, endpoint or limit is written in the code; the only default for the Keeper URL lives in `.env.example`.

| Variable                        | Required       | Meaning                                                                                                              |
| ------------------------------- | -------------- | -------------------------------------------------------------------------------------------------------------------- |
| `VITE_API_MODE`                 | yes            | `mock` (in-memory sample Domains) or `live` (Keeper over REST)                                                       |
| `VITE_KEEPER_BASE_URL`          | in `live` mode | Keeper origin without `/api/v1`, for example `http://localhost:8080`, or `/` for the app's own origin behind a proxy |
| `VITE_API_TOKEN`                | no             | API key or access token sent as `Authorization: Bearer`; development only (see [Authentication](#authentication))    |
| `VITE_PAGE_SIZE`                | no (50)        | Rows per page in Record lists                                                                                        |
| `VITE_SEARCH_LIMIT`             | no (20)        | Results per search                                                                                                   |
| `VITE_CANVAS_DEPTH`             | no (2)         | Neighborhood depth for the Canvas graph explorer                                                                     |
| `VITE_CANVAS_MAX_NODES`         | no (200)       | Node cap for one Canvas neighborhood                                                                                 |
| `VITE_CANVAS_LAYOUT_ITERATIONS` | no (100)       | ForceAtlas2 iterations when laying out a graph                                                                       |
| `VITE_QUERY_STALE_MS`           | no (30000)     | How long cached reads count as fresh                                                                                 |
| `VITE_TOAST_MS`                 | no (5000)      | How long a toast stays on screen                                                                                     |
| `VITE_MOCK_LATENCY_MS`          | no (0)         | Artificial delay of every mock call                                                                                  |
| `VITE_SCHEMA_BRANCH_PREFIX`     | no (`schema/`) | Prefix of the Branch that carries schema changes of a protected one                                                  |

`LOREBASE_KEEPER_PROXY` is not a `VITE_` variable and never reaches the bundle: it is read by `vite.config.ts` for `pnpm dev` and `pnpm preview` and makes them proxy `/api` to that Keeper (see [Live mode](#live-mode)). The optional values are non-functional tunables with defaults in `src/config/env.ts`. A missing or invalid required value stops the app at start with a message on screen. `src/config/env.ts` is the only place that reads the environment; components get the validated config from `useConfig()`.

## Mock mode

`VITE_API_MODE=mock` serves two sample Domains from memory: `supply` (Suppliers, Parts, Plants, Contracts; the demo Domain of the glossary) with several Branches, Proposals, Conflicts and extraction items, and `hr`.

The mock is a plain object that implements the same `KeeperApi` interface as the HTTP adapter, not a service worker. Reasons: no extra dependency and no worker file to ship, it runs unchanged under Vitest and in a static build, and it keeps Branch state mutable for the session so Review actions can be tried. What it does:

- Reads: Domains, Branches (with ahead and behind counts against the default Branch), log, schema per Branch, Records with the query DSL (`where` with `and`, `or`, `not`, `via` and `derived`, ordering, cursor paging), Links in both directions, Lineage, history, search, neighborhood, diff between Branches.
- Writes: create and delete Branches, create, review, close and merge Proposals, resolve Conflicts, decide extraction items, create Domains, commit schema change sets.
- Merge attempts: Proposal 41 (`model/risk` into `main`) carries six Conflicts of six kinds (`SC1`, `DV1`, `DV2`, `XS4`, `XS5`, `XS9`) and one automatic policy Resolution; Proposal 43 carries one `DV1`. The Conflicts are seeded, not recomputed from the Branches. Resolutions are stored by fingerprint and checked against the Conflict (choice allowed for its kind, `value` or `values` only on `custom`, a value for every member of a group). `POST .../merge` applies them to a copy of the source state, schema changes first, and re-validates custom values against the merged schema: a refused Resolution is dropped and its reason is kept on the Conflict (`details.rejected`). It answers `merged` with a merge Revision (parents: target head, source head) or `conflicts` with the attempt that remains. The target does not change until every Conflict is resolved.
- Schema change sets: `applyChanges` runs the commands on the Branch schema, checks the structural rules (names, references, types, constraints, cardinality, Trait mappings), migrates the stored Records and Links (casts with the safe, checked and forbidden rules, defaults, dropped Attributes and Kinds) and reports the migrations. A checked cast that fails on a stored value, or a new required Attribute without a default, makes the set invalid and commits nothing. A protected Branch refuses a commit (409 `protected-branch`) but answers a dry run. Built-in Operations are generated for new Kinds.
- Not emulated, answered with a 501 problem: committing an Operation (dry run works), traversal, Trait queries. Stale merge attempts (target head moved while resolving) are not modeled.

Sample data lives in `src/api/mock/sample/`. To point the app at a real Keeper, see [Live mode](#live-mode); no screen changes.

## API types

Two layers. `src/api/generated/keeper.ts` is the wire layer: types produced from `docs/openapi.json` by `openapi-typescript` (`pnpm types`; the dev dependency is there because the API contract is the single source of truth and hand-copying 90 schemas drifts). It is committed, and `src/api/generated/generated.test.ts` regenerates it in memory and fails when the file is stale, so after `make openapi` run `pnpm types` and commit both files.

`src/api/types.ts` keeps the names the screens use. Where a type maps one to one onto the contract it is an alias of it (`Domain`, `Proposal`, `ProposalDetail`, the review, approval and check types, `CreateBranchInput`, `WhoAmI`, `Webhook`, `ContextPack`, and so on). The rest are UI-facing view models that the adapters produce: `Schema` (lists of elements, types spelled `decimal(12,2)`, see `src/api/live/schema.ts`), `RecordRow` (`Kind/key` references and values by Attribute api name), `Revision`, `Branch` (with its distance to the default Branch), `DiffResponse`, `MergeAttempt` and the Conflict types. The `KeeperApi` interface isolates the screens from both.

## Folder structure

```
src/
  main.tsx              entry: config check, providers, router
  router.tsx            router factory (browser or memory history)
  routeTree.gen.ts      written by the router plugin on dev and build; committed so tests and typecheck work
  routes/               file-based routes; thin, each renders a feature page
  config/               env loading and validation, config context
  api/
    generated/          types generated from docs/openapi.json (pnpm types)
    types.ts            contract aliases and the view models of the screens
    client.ts           the KeeperApi interface (one per resource area)
    http.ts             the live adapter: every KeeperApi method over the Keeper routes
    fetcher.ts          fetch layer: base path, auth header, Idempotency-Key, ETag, problems
    errors.ts           ApiError and its typed subclasses, describeError for toasts
    credentials.ts      the API token of the session (VITE_API_TOKEN or typed in Settings)
    live/               mappers from Keeper responses to view models (schema, data, versioning)
    mock/               in-memory adapter, query interpreter, sample Domains
    queries.ts          TanStack Query hooks and Revision-aware cache keys
    provider.tsx        context that supplies the active adapter
  app/                  shell: top bar, Domain and Branch switchers, global search, left navigation, scope
  features/
    explorer/           Domain home, search, Kind table, Record page
    canvas/             graph explorer and schema graph (Sigma.js)
    modeler/            schema overview, schema editor (form and YAML over one document)
    review/             Proposal list and detail
    settings/           appearance, runtime configuration, Domain settings
    inventory.ts        screen inventory of design 12.3, source of the planned-screen placeholders
  ui/                   small accessible primitives: button, field (input, select), dialog, table, tabs, toast, layout helpers, theme
  test/                 test setup and render helper
e2e/                    Playwright flows, global setup (stack, sample, scenario), compose config
scripts/                screenshots (mock or live)
```

## How it fits together

- **Branch is first class.** The Domain is a path segment (`/supply/...`); Branch and Revision are search params (`?branch=model%2Frisk&at=<revision>`) shared by every screen. The top bar shows the Branch switcher with the head Revision and how far the Branch is behind the default Branch. Navigation links carry the context along.
- **Scope.** `ScopeProvider` resolves the Domain and Branch named by the URL and exposes `{domain, branch, head, at}` through `useScope()`. Screens render only when both exist.
- **Revision-aware caching.** Every scoped query key includes the Branch head Revision (or `at`), so a Branch that moves never serves old reads as current.
- **Theme.** Colors are CSS variables in `src/styles.css`, mapped to Tailwind utilities (`bg-surface`, `text-muted`, `border-line`, ...). Light and dark follow the system by default and can be forced from the top bar or Settings; the choice is kept in local storage.
- **Canvas.** Sigma 4 needs WebGL 2. It is loaded on demand; when WebGL is not available (tests, some remote desktops) the canvas shows a message and the list view below still describes the graph. Layout is ForceAtlas2 run synchronously on the main thread (a web worker is planned for large graphs).

## Screens

Built with real data from the active adapter: Explorer (Domain home, search with Kind filter, Kind table with sort and paging, Record page), Canvas (graph explorer, schema graph), Modeler (Kinds, Attributes, Relations, Traits, Operations, Derivations, Sources lists, and the schema editor), Review (Proposal list with Conflict counts, Proposal detail with checks, reviews and approvals, a review form, merge attempt and Conflict resolver, data diff, merge and close), Settings (appearance, connection and token, who Keeper says you are, Domain and runtime values). The top bar has a Branch switcher and a "New Branch" button that starts a Branch at the head of the one on screen.

Every other screen of the inventory in design 12.3 (Trait view, diff overlay, Operation, Derivation and Source mapping editors, extraction items, Sources, History, Admin) appears as a placeholder section marked "Planned", built from `src/features/inventory.ts`.

## Conflict resolver

The merge attempt of a Proposal (design 6.8 to 6.10, 12.3) is the "Merge attempt" section of the Proposal detail, `src/features/review/`.

- **List.** Conflicts grouped by kind (`DV1` to `XS11`, with their names and counts), filters by kind, status (open, resolved) and text, a progress bar, and the Conflicts a policy already resolved. The selected Conflict is in the URL (`?conflict=<fingerprint>`), so a Conflict can be linked. The list is one listbox: arrow keys or `j` and `k` move between Conflicts, `Home` and `End` jump to the ends, "Next open Conflict" skips the resolved ones.
- **Resolver.** Base, ours and theirs side by side. Records are compared attribute by attribute and schema elements property by property (dotted paths, ids replaced by api names); only the paths where a side moved from the base are listed, with the rest one checkbox away. Absent sides (a deleted Record) are shown as absent. The choices are the ones the Conflict carries (`choices`, from `CHOICES` of its kind), never a fixed list.
- **Custom values.** The input follows the type: number field with the constraints of the Attribute, select for enums and booleans, date field, text area for long text, JSON text for Records, lists and schema documents. A value that breaks the type or the constraints, or JSON that does not parse, blocks "Save resolution" and says why. Types come from the schema of the source Branch.
- **Grouped Conflicts** (`XS1` to `XS9`) list their members with the values each side holds, take one value per member (`values` keyed by member key), offer "same value for every member", and accept the choices that need no value for the whole group.
- **Bulk.** One choice for every open Conflict of the current filter that accepts it; the button says how many it touches.
- **Apply resolutions** calls `POST .../merge`. Merged: the page shows the merged Revision (id, parents marked target and source, changes), the Resolutions applied and the policy resolutions. Conflicts remain: the list shows them, and a Resolution the merge run refused on re-validation is dropped and its reason appears on the Conflict.

Against Keeper (verified by `e2e/04-conflicts.spec.ts`): `GET .../conflicts` answers with the attempt and one page of Conflicts, `POST .../conflicts:resolve` stores Resolutions and answers with the re-run attempt, and `POST .../merge` answers 409 `merge-conflicts` with the attempt while Conflicts remain (the adapter turns that into `{ status: 'conflicts', attempt }`) and 409 `approvals-required` until the protected target has its approvals (Keeper does not take a merge into a protected Branch without them; the Proposal page has the review form for that). Keeper names a data partition `k:{kind id}` or `r:{relation id}` and an Attribute by id; the screens translate both with the schema of the source Branch (`partitionName`, `messageOf` in `conflict-meta.ts`). Values are in the canonical form of Keeper (design 6.1): integers and decimals are strings (`"80"`, `"24.01"`), and a custom value must be canonical too, so the number field produces `"55"` and pads decimals to the scale of the type (`canonicalNumber` in `src/api/values.ts`); a number is accepted in the sample data of the mock all the same.

## Schema editor

The Modeler's "Edit schema" view (`?view=edit`, `src/features/modeler/`) edits the schema of the current Branch as one document in two synchronized views.

- **Document.** The layout of `packages/core/src/lorebase_core/model/yaml_schema.py`: `format`, `domain`, then `kinds`, `relations`, `traits`, `operations`, `derivations` and `sources` keyed by api name, references by api name, `id` written always and honored on input (an explicit id keeps the element through a rename; without one the element with the same api name on the Branch is reused, else an id is minted). Types are written `string`, `decimal(12,2)`, `ref(Kind)`, `{enum: [...]}`, `{array: ...}`. Defaults are left out. `document.ts` converts between that layout and the id-keyed `Schema` of the API; built-in Operations are generated by Keeper and are not part of the document.
- **Form.** Kinds (name, status, primary key, title Attribute, Attributes with type, enum values, required, unique, indexed and constraints, Trait implementations with their mapping), Relations (endpoints, inverse name, cardinality, delete behavior, required, link Attributes), Traits (Attributes, implementing Kinds). Renaming a Kind or an Attribute rewrites the references the document keeps to it. Operations, Derivations and Sources are YAML only for now; the form lists the Operations.
- **YAML.** CodeMirror 6 with the YAML language, loaded on demand in its own chunk so the rest of the Modeler does not pay for it. `yaml` parses the text and writes it back. Typing valid YAML replaces the document; a form edit rewrites the text. While the text does not parse the form keeps the last valid document and is read only, and the syntax error is listed with its line.
- **Validation.** `src/api/schema-rules.ts` checks names, duplicates, references, types, constraints, cardinality, Trait mappings, Operation steps and Source connections. Every issue has a path in the layout of the document (`kinds/Supplier/attributes/tier/type`); it appears under the field it belongs to and in the issues list, where "Line N" moves the cursor of the YAML editor there. The same rules run in the mock before it accepts a change set.
- **Change set.** `diffSchemas` turns the edit into commands (`add_kind`, `update_attribute`, `remove_relation`, and so on, in `src/api/schema-changes.ts`); the summary lists them and shows the data migrations a dry run on the Branch reports (casts, defaults, dropped values). On an unprotected Branch "Save change set" commits a schema Revision. A protected Branch cannot take it: "Open Proposal" creates a Branch (prefix `VITE_SCHEMA_BRANCH_PREFIX`), commits the change set there and opens a Proposal against the protected Branch.

Keeper takes a change set as a document, not as commands: `POST .../branches/{b}/schema/changes` with the whole schema as `yaml` (and `dry_run`, `message`, `cascade`), `POST .../schema:validate` for a check that writes nothing. The live adapter therefore sends the document the editor holds (`SchemaChangeRequest.document`) and ignores `changes`; the commands drive the mock and the summary list. A change set Keeper refuses answers 422 with `issues`, which the adapter returns as `valid: false` with the findings (path as `kinds/Supplier/attributes/tier`), so the editor shows Keeper's own rules next to its local ones: an Attribute without a description, an id that is not a ULID, and so on. The Schema from Keeper is mapped to the view model and back to the same document without a difference (`src/api/live/schema.test.ts` checks it against a real schema and its YAML), and `e2e/03-schema.spec.ts` checks that an untouched schema produces an empty change set. Element ids minted by the editor are ULIDs with a first character 0 to 7, which is what Keeper accepts.

## Live mode

`VITE_API_MODE=live` makes the app talk to Keeper over `/api/v1`; `VITE_KEEPER_BASE_URL` says where. Two ways to reach it from a browser:

- **Same origin, through the dev proxy (recommended).** Set `VITE_KEEPER_BASE_URL=/` and start the app with `LOREBASE_KEEPER_PROXY=http://localhost:8080 pnpm dev` (or `pnpm preview`). The browser only talks to the app's origin, no CORS setup is needed, and the `ETag` header of Keeper is readable, so reads are conditional (below).
- **Cross origin.** Set `VITE_KEEPER_BASE_URL=http://localhost:8080` and let Keeper allow the app's origin: `keeper.cors.allow_origins: [http://localhost:5173]` and `keeper.cors.allow_headers` with `If-None-Match` added. Keeper does not expose `ETag` to cross-origin scripts, so conditional reads do not happen then (every read is a plain 200); everything else works.

To try it against the compose stack of `docs/quickstart.md`: start Postgres and Keeper, load a Domain with the `lorebase` CLI (the quickstart's steps 6 and 7), then

```bash
cd apps/web
printf 'VITE_API_MODE=live\nVITE_KEEPER_BASE_URL=/\n' > .env.local
LOREBASE_KEEPER_PROXY=http://localhost:8080 pnpm dev
```

### Authentication

Keeper in `keeper.auth.mode: none` (development) needs nothing. With `api_key` or `oidc`, set `VITE_API_TOKEN` or type the token into Settings, Connection: the adapter sends `Authorization: Bearer <token>` on every request. The token typed in Settings is kept in this browser's local storage and wins over `VITE_API_TOKEN`; "Forget stored token" removes it. **Both are development conveniences.** A `VITE_` variable is part of the bundle, and local storage is readable by any script on the origin, so neither belongs in a deployment that other people use; put the app behind a login or a proxy that adds the header instead. Settings, "Who am I", shows what Keeper (`GET /auth/whoami`) makes of the credential.

### What the adapter does

- **Every method of `KeeperApi` has a route.** Domains, Branches (and tags), the schema (`GET`, `schema:validate`, change sets), Records, Links, query, traverse, neighborhood, search, the Branch log, Revisions, diff, Proposals with reviews, merge, Conflicts and Resolutions, Operation execution, `context`, `explain`, schema inference upload (`POST .../schema:infer`, multipart) and webhooks. Extraction items have no Keeper route yet: the list is empty and deciding items answers 501.
- **Errors are problem details, typed.** `ApiError` has subclasses by code and status (`ProtectedBranchError`, `ApprovalsRequiredError`, `MergeConflictsError`, `ValidationError` with its `issues`, `NotFoundError`, `ForbiddenError`, `UnauthorizedError`, `ConflictError`, `UnavailableError`, `NetworkError` for no answer at all). The query client shows every failed read and write as a toast built by `describeError` (the problem detail, the first finding of a refused request, a hint to set the token on 401).
- **Conditional reads.** The fetch layer keeps the last answer per URL and credential and sends `If-None-Match`; a 304 serves the kept body. Keeper binds the `ETag` to the Revision, so an unchanged Branch costs no body.
- **Idempotent creates.** Creating a Domain, Branch or Proposal and merging a Proposal send an `Idempotency-Key` (a fresh UUID per call), and executing an Operation does too unless the request carries its own key. Keeper answers a create that carries a key with 200 and the existing resource when the name is taken by the same caller; since the keys are fresh, that is never a retry of ours, and the adapter reports it as `already-exists`.
- **Values.** Keeper writes integers and decimals as strings. The screens show them as they are, and the type-aware checks accept both forms.

### Limits against Keeper

- **Record history and lineage come from `explain`.** Keeper keeps no per-Record list of Revisions. History lists the writes `explain` knows (one per distinct write time, with the Attributes written then), and lineage lists the provenance of each Attribute. `explain` stamps every Attribute with the Revision that last touched the Record, so the adapter takes the creating Revision for the Attributes written at creation. Neither works at a past Revision (`at`): those answer 501.
- **Derived values** are not served by Keeper yet; a derived Attribute shows no value.
- **The built-in Operations** are not part of the schema Keeper serves (they are generated); the Operations list shows the stored ones.
- **Schema elements the editor does not model** are carried as they are when the document is written back (`status`, `watermark_column`, the `domain` block, descriptions of Trait Attributes); anything else a future schema version adds would not survive a save, which is why the round trip test compares against a real schema.
- **Source mappings, Derivations and Operations** are YAML only in the editor.

## Tests

`pnpm test` runs a router smoke test, one component test per screen, tests for the API adapters, the fetch layer, the typed errors and the config loader, and tests for the UI primitives. The Conflict resolver has component tests for choice application, custom values and grouped values, and app-level tests for the merge attempt flow; the schema editor has tests for the form and YAML sync (the YAML view is driven through its CodeMirror state), validation, change sets and the protected Branch flow; the mock has tests for merge attempts, Resolutions and schema change sets. Component tests render the whole app against the mock adapter with an in-memory router history, so they exercise routing, URL state and data loading together. The live adapter has tests against a scripted Keeper (`src/api/http.test.ts`), and the schema mapper is checked against a real schema and its YAML (`src/api/live/fixtures/`, taken from a Keeper that loaded `examples/supply`). `src/api/generated/generated.test.ts` fails when the generated types are older than `docs/openapi.json`.

### End-to-end tests

`pnpm e2e` builds the app in live mode, serves it with `vite preview` and a same-origin proxy to Keeper, and runs the flows in `e2e/` in Chromium against a real Keeper:

| Spec           | Flow                                                                                                     |
| -------------- | -------------------------------------------------------------------------------------------------------- |
| `01-browse`    | the Supplier S-1001 in Explorer (table, Record page, Links, Lineage), its Canvas, search                 |
| `02-branch`    | create a Branch from the top bar; a refused name is a toast                                              |
| `03-schema`    | add an Attribute in the Modeler on protected `main`: Keeper's dry run, Proposal, review, merge           |
| `04-conflicts` | two Branches edit the same Attributes; inspect the Conflicts, resolve (a custom value and a side), merge |
| `05-settings`  | connection, who Keeper says you are, a stored token is sent as a Bearer header                           |

The global setup (`e2e/global-setup.ts`) starts the stack, loads the supply sample into a Domain named for the run (the schema without the three elements that use expressions, then the CSV files through `import_records` and `import_links`), and prepares the Conflict scenario with the REST API: `model/risk` and `source/erp` change the same two Supplier Attributes, the first Proposal is approved and merged, the second stays open. The Domain is dropped, or the stack removed, at the end.

Without `LOREBASE_E2E_URL` the stack is `deploy/compose/docker-compose.yaml` under the project name `lorebase-e2e`, with its own image name and host ports, a Keeper configuration from `e2e/keeper.e2e.yaml` (authentication off, self-approval on), built from the repository: it needs Docker and a few minutes the first time. With `LOREBASE_E2E_URL=http://127.0.0.1:8080` it uses a Keeper that is already running (CI starts one from the checkout with `uv run python -m lorebase_keeper` on a Postgres service, no Docker inside the job); that Keeper must have `keeper.proposals.allow_self_approval: true` and authentication off.

| Variable                     | Default        | Meaning                                       |
| ---------------------------- | -------------- | --------------------------------------------- |
| `LOREBASE_E2E_URL`           | unset          | Keeper to use; no stack is started or stopped |
| `LOREBASE_E2E_KEEPER_PORT`   | `18480`        | Host port of the stack's Keeper               |
| `LOREBASE_E2E_POSTGRES_PORT` | `15532`        | Host port of the stack's Postgres             |
| `LOREBASE_E2E_WEB_PORT`      | `15174`        | Port of the app under test                    |
| `LOREBASE_E2E_PROJECT`       | `lorebase-e2e` | Compose project name                          |
| `LOREBASE_E2E_IMAGE`         | `lorebase-e2e` | Image name the stack builds                   |
| `LOREBASE_E2E_KEEP`          | unset          | `1` leaves the stack running after the run    |

```bash
pnpm exec playwright install chromium   # once per machine
pnpm e2e                                # builds the stack, runs, removes it
LOREBASE_E2E_URL=http://127.0.0.1:8080 pnpm e2e -g "conflicts"   # one flow, Keeper already running
```

Reports and traces of a failed run are in `node_modules/.cache/e2e/`.
