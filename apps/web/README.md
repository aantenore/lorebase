# Lorebase web

The web application of Lorebase: Explorer, Canvas, Modeler and Review over the Keeper REST API (docs/design.md, sections 11 and 12). It uses only the public REST API, and runs without a backend in mock mode.

Stack: React 19, TypeScript (strict), Vite, Tailwind CSS 4, TanStack Router (file-based) and Query, Sigma.js with graphology, Vitest with Testing Library.

## Run

Requirements: Node.js 24 and pnpm.

```bash
cd apps/web
pnpm install
cp .env.example .env     # mock mode by default
pnpm dev                 # http://localhost:5173
```

| Script           | What it does                                   |
| ---------------- | ---------------------------------------------- |
| `pnpm dev`       | Vite dev server with HMR                       |
| `pnpm build`     | Type-check, then production build into `dist/` |
| `pnpm preview`   | Serve the production build locally             |
| `pnpm test`      | Unit and component tests (Vitest, jsdom)       |
| `pnpm typecheck` | `tsc --noEmit`                                 |
| `pnpm lint`      | ESLint (type-aware) and Prettier check         |
| `pnpm format`    | Prettier write                                 |

While iterating, run only the tests that cover what you changed, for example `pnpm test src/features/review`.

## Configuration

Everything comes from Vite environment variables. No URL, endpoint or limit is written in the code; the only default for the Keeper URL lives in `.env.example`.

| Variable                        | Required       | Meaning                                                              |
| ------------------------------- | -------------- | -------------------------------------------------------------------- |
| `VITE_API_MODE`                 | yes            | `mock` (in-memory sample Domains) or `live` (Keeper over REST)       |
| `VITE_KEEPER_BASE_URL`          | in `live` mode | Keeper origin without `/api/v1`, for example `http://localhost:8080` |
| `VITE_PAGE_SIZE`                | no (50)        | Rows per page in Record lists                                        |
| `VITE_SEARCH_LIMIT`             | no (20)        | Results per search                                                   |
| `VITE_CANVAS_DEPTH`             | no (2)         | Neighborhood depth for the Canvas graph explorer                     |
| `VITE_CANVAS_MAX_NODES`         | no (200)       | Node cap for one Canvas neighborhood                                 |
| `VITE_CANVAS_LAYOUT_ITERATIONS` | no (100)       | ForceAtlas2 iterations when laying out a graph                       |
| `VITE_QUERY_STALE_MS`           | no (30000)     | How long cached reads count as fresh                                 |
| `VITE_TOAST_MS`                 | no (5000)      | How long a toast stays on screen                                     |
| `VITE_MOCK_LATENCY_MS`          | no (0)         | Artificial delay of every mock call                                  |

The optional values are non-functional tunables with defaults in `src/config/env.ts`. A missing or invalid required value stops the app at start with a message on screen. `src/config/env.ts` is the only place that reads the environment; components get the validated config from `useConfig()`.

## Mock mode

`VITE_API_MODE=mock` serves two sample Domains from memory: `supply` (Suppliers, Parts, Plants, Contracts; the demo Domain of the glossary) with several Branches, Proposals, Conflicts and extraction items, and `hr`.

The mock is a plain object that implements the same `KeeperApi` interface as the HTTP adapter, not a service worker. Reasons: no extra dependency and no worker file to ship, it runs unchanged under Vitest and in a static build, and it keeps Branch state mutable for the session so Review actions can be tried. What it does:

- Reads: Domains, Branches (with ahead and behind counts against the default Branch), log, schema per Branch, Records with the query DSL (`where` with `and`, `or`, `not`, `via` and `derived`, ordering, cursor paging), Links in both directions, Lineage, history, search, neighborhood, diff between Branches.
- Writes: create and delete Branches, create, review, close and merge Proposals, resolve Conflicts, decide extraction items, create Domains, commit schema change sets.
- Merge attempts: Proposal 41 (`model/risk` into `main`) carries six Conflicts of six kinds (`SC1`, `DV1`, `DV2`, `XS4`, `XS5`, `XS9`) and one automatic policy Resolution; Proposal 43 carries one `DV1`. The Conflicts are seeded, not recomputed from the Branches. Resolutions are stored by fingerprint and checked against the Conflict (choice allowed for its kind, `value` or `values` only on `custom`, a value for every member of a group). `POST .../merge` applies them to a copy of the source state, schema changes first, and re-validates custom values against the merged schema: a refused Resolution is dropped and its reason is kept on the Conflict (`details.rejected`). It answers `merged` with a merge Revision (parents: target head, source head) or `conflicts` with the attempt that remains. The target does not change until every Conflict is resolved.
- Schema change sets: `applyChanges` runs the commands on the Branch schema, checks the structural rules (names, references, types, constraints, cardinality, Trait mappings), migrates the stored Records and Links (casts with the safe, checked and forbidden rules, defaults, dropped Attributes and Kinds) and reports the migrations. A checked cast that fails on a stored value, or a new required Attribute without a default, makes the set invalid and commits nothing. A protected Branch refuses a commit (409 `branch_protected`) but answers a dry run. Built-in Operations are generated for new Kinds.
- Not emulated, answered with a 501 problem: committing an Operation (dry run works), traversal, Trait queries. Stale merge attempts (target head moved while resolving) are not modeled.

Sample data lives in `src/api/mock/sample/`. To point the app at a real Keeper, set `VITE_API_MODE=live` and `VITE_KEEPER_BASE_URL`; nothing else changes.

## API types

The types in `src/api/types.ts` are maintained by hand against the Keeper design (design 11.1). Once Keeper publishes its OpenAPI document (`/api/v1/openapi.json`), they are meant to be replaced by types produced from it at build time; the `KeeperApi` interface isolates the rest of the app from that change.

## Folder structure

```
src/
  main.tsx              entry: config check, providers, router
  router.tsx            router factory (browser or memory history)
  routeTree.gen.ts      written by the router plugin on dev and build; committed so tests and typecheck work
  routes/               file-based routes; thin, each renders a feature page
  config/               env loading and validation, config context
  api/
    types.ts            Keeper types mirroring the glossary and design 5, 6, 8, 11
    client.ts           the KeeperApi interface (one per resource area)
    http.ts             fetch adapter for /api/v1, RFC 9457 problems as ApiError
    mock/               in-memory adapter, query interpreter, sample Domains
    queries.ts          TanStack Query hooks and Revision-aware cache keys
    provider.tsx        context that supplies the active adapter
  app/                  shell: top bar, Domain and Branch switchers, global search, left navigation, scope
  features/
    explorer/           Domain home, search, Kind table, Record page
    canvas/             graph explorer and schema graph (Sigma.js)
    modeler/            schema overview
    review/             Proposal list and detail
    settings/           appearance, runtime configuration, Domain settings
    inventory.ts        screen inventory of design 12.3, source of the planned-screen placeholders
  ui/                   small accessible primitives: button, field (input, select), dialog, table, tabs, toast, layout helpers, theme
  test/                 test setup and render helper
```

## How it fits together

- **Branch is first class.** The Domain is a path segment (`/supply/...`); Branch and Revision are search params (`?branch=model%2Frisk&at=<revision>`) shared by every screen. The top bar shows the Branch switcher with the head Revision and how far the Branch is behind the default Branch. Navigation links carry the context along.
- **Scope.** `ScopeProvider` resolves the Domain and Branch named by the URL and exposes `{domain, branch, head, at}` through `useScope()`. Screens render only when both exist.
- **Revision-aware caching.** Every scoped query key includes the Branch head Revision (or `at`), so a Branch that moves never serves old reads as current.
- **Theme.** Colors are CSS variables in `src/styles.css`, mapped to Tailwind utilities (`bg-surface`, `text-muted`, `border-line`, ...). Light and dark follow the system by default and can be forced from the top bar or Settings; the choice is kept in local storage.
- **Canvas.** Sigma 4 needs WebGL 2. It is loaded on demand; when WebGL is not available (tests, some remote desktops) the canvas shows a message and the list view below still describes the graph. Layout is ForceAtlas2 run synchronously on the main thread (a web worker is planned for large graphs).

## Screens

Built with real data from the active adapter: Explorer (Domain home, search with Kind filter, Kind table with sort and paging, Record page), Canvas (graph explorer, schema graph), Modeler (Kinds, Attributes, Relations, Traits, Operations, Derivations, Sources lists), Review (Proposal list with Conflict counts, Proposal detail with checks, reviews, merge attempt and Conflict resolver, data diff, merge and close), Settings.

Every other screen of the inventory in design 12.3 (Trait view, diff overlay, editors, extraction items, Sources, History, Admin) appears as a placeholder section marked "Planned", built from `src/features/inventory.ts`.

## Conflict resolver

The merge attempt of a Proposal (design 6.8 to 6.10, 12.3) is the "Merge attempt" section of the Proposal detail, `src/features/review/`.

- **List.** Conflicts grouped by kind (`DV1` to `XS11`, with their names and counts), filters by kind, status (open, resolved) and text, a progress bar, and the Conflicts a policy already resolved. The selected Conflict is in the URL (`?conflict=<fingerprint>`), so a Conflict can be linked. The list is one listbox: arrow keys or `j` and `k` move between Conflicts, `Home` and `End` jump to the ends, "Next open Conflict" skips the resolved ones.
- **Resolver.** Base, ours and theirs side by side. Records are compared attribute by attribute and schema elements property by property (dotted paths, ids replaced by api names); only the paths where a side moved from the base are listed, with the rest one checkbox away. Absent sides (a deleted Record) are shown as absent. The choices are the ones the Conflict carries (`choices`, from `CHOICES` of its kind), never a fixed list.
- **Custom values.** The input follows the type: number field with the constraints of the Attribute, select for enums and booleans, date field, text area for long text, JSON text for Records, lists and schema documents. A value that breaks the type or the constraints, or JSON that does not parse, blocks "Save resolution" and says why. Types come from the schema of the source Branch.
- **Grouped Conflicts** (`XS1` to `XS9`) list their members with the values each side holds, take one value per member (`values` keyed by member key), offer "same value for every member", and accept the choices that need no value for the whole group.
- **Bulk.** One choice for every open Conflict of the current filter that accepts it; the button says how many it touches.
- **Apply resolutions** calls `POST .../merge`. Merged: the page shows the merged Revision (id, parents marked target and source, changes), the Resolutions applied and the policy resolutions. Conflicts remain: the list shows them, and a Resolution the merge run refused on re-validation is dropped and its reason appears on the Conflict.

Keeper does not publish the merge attempt endpoints yet beyond design 11.1, so two assumptions are marked here: `GET .../conflicts` answers with the whole attempt (Conflicts, Resolutions by fingerprint, automatic Resolutions, base, ours and theirs Revisions), and `POST .../merge` answers 200 with `status: merged` or `status: conflicts` instead of failing while Conflicts remain.

## Tests

`pnpm test` runs a router smoke test, one component test per screen, tests for the API adapters and the config loader, and tests for the UI primitives. Component tests render the whole app against the mock adapter with an in-memory router history, so they exercise routing, URL state and data loading together.
