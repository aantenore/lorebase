# Visual tour

Screenshots of the Lorebase web app (`apps/web`), taken at 1440x900 in the light theme.

**Live and mock.** The pictures below come from the app in live mode against a running Keeper: Postgres and Keeper from `deploy/compose`, the `examples/supply` sample loaded through the REST API (30 Suppliers, 40 Parts, 20 Plants and their Links), and a Proposal with two Conflicts left open. They were taken with `pnpm screenshots --live`. The same screens run on an in-memory mock of the Keeper API when `VITE_API_MODE=mock` (two sample Domains, no backend), which is what the unit tests and `pnpm screenshots` without `--live` use. The flows behind the pictures are exercised against a real Keeper by the Playwright suite in `apps/web/e2e` (browse a Supplier and its Canvas, create a Branch, change the schema through a Proposal, resolve Conflicts and merge, Settings), which CI runs on every change; see [Status](../README.md#status-and-roadmap) for what is still planned. Not everything is served yet: Keeper has no per-Record history list (the Record page lists the writes `explain` knows), no derived values and no extraction items, and the screens say so where it matters. The animation below was recorded in mock mode.

![Explorer, a Record, the Canvas and the Review resolver in one pass](images/tour.gif)

The images are regenerated with `pnpm screenshots --live --keeper <url>` in `apps/web` (without `--live`, from the mock), and the animation above with `pnpm screenshots flow`; see [apps/web/README.md](../apps/web/README.md#screenshots).

## Explorer

### Domain home

![Explorer, Domain home](images/explorer-home.png)

The entry point of a Domain on a Branch. It offers search over Records with a Kind filter, the Kinds of the Domain, the latest Revisions of the Branch (with their origin, such as `import`, `operation` or `schema_change`) and the Proposals waiting for review. The Domain and Branch switchers and the head Revision in the top bar apply to every screen; "New Branch" starts a Branch at the head of the one on screen. Screens listed under "planned" at the bottom are placeholders.

### Kind table

![Explorer, Kind table](images/explorer-kind.png)

The Records of one Kind as a table, with a column per Attribute, sorting and cursor paging. Each row opens the Record page. Keeper writes integers and decimals as strings (risk score `"32"`); the table shows them as numbers all the same.

### Record page

![Explorer, Record page](images/explorer-record.png)

One Record with its Attribute values, the Relations it takes part in (Links in both directions, each one navigable) and its Lineage: for every Attribute, where the value came from, who wrote it and in which Revision. "Open in Canvas" jumps to the graph around the Record. Lineage and the history below it are built from Keeper's `explain` endpoint.

## Canvas

### Graph explorer

![Canvas, graph explorer](images/canvas-graph.png)

The neighborhood of a Record drawn as a graph (Keeper's `neighborhood` endpoint): nodes are Records coloured by Kind, edges are Links, and a click shows the details of the selected node. Depth and node cap are configuration values. The drawing uses Sigma.js, which needs WebGL 2; where it is missing, the screen shows a message and a list view of the same nodes and edges, which is also available under the graph. The image above was captured with the WebGL canvas rendering (software GL in headless Chromium). A worker-based layout for large graphs is planned.

### Schema graph

![Canvas, schema graph](images/canvas-schema.png)

The schema of the Branch as a graph: Kinds as nodes, Relations as edges, with the same selection panel and an "Open in Modeler" link. It is a quick way to see how the Domain is shaped before editing it. The third tab of the Canvas, the diff overlay, is not covered here.

## Modeler

### Schema overview

![Modeler, list view](images/modeler-list.png)

The schema of the current Branch as lists: Kinds, Attributes, Relations, Traits, Operations, Derivations and Sources, with counts. On a protected Branch such as `main` the banner says that schema changes go through another Branch and a Proposal. Operation, Derivation and Source mapping editors are marked "Planned".

### Edit schema

![Modeler, schema editor](images/modeler-edit.png)

The schema as one document in two synchronized views: a form for Kinds, Relations and Traits and a YAML editor (CodeMirror) for everything, including Operations, Derivations and Sources. Validation issues point at the field and the YAML line, and include Keeper's own: the editor asks Keeper to dry-run the edited document, so an Attribute without a description or an unknown reference is refused there and shown next to the local findings. The change set summary shows the commands and the data migrations the dry run reports. On a protected Branch "Open Proposal" creates a Branch, commits the document on it and opens the Proposal against `main`; on any other Branch "Save change set" commits a schema Revision. An untouched schema produces an empty change set.

## Review

### Proposal list

![Review, Proposal list](images/review-list.png)

The Proposals of the Domain with origin (manual or extraction), the Branches they merge, status and the number of unresolved Conflicts, filterable by status. The Proposal page also has the checks Keeper runs, the reviews with where the approvals stand (a protected target wants approvals before it takes a merge) and a form to add one. The extraction items screen is planned.

### Merge attempt and Conflict resolver

![Review, Conflict resolver](images/review-conflict.png)

Two Branches changed the same Supplier Attributes (the risk score of S-1001 and the tier of S-1002), and the Proposal that merges the second ends up with two Conflicts of kind `DV1`. Conflicts are grouped by kind, filterable, and one is selected in the URL; they are named by Kind, key and Attribute, not by the ids Keeper uses. The resolver shows base, ours and theirs side by side and offers only the resolutions that fit the Conflict's kind (take ours, take theirs, revert to base, custom value); grouped Conflicts take one value per member. "Apply resolutions" asks Keeper to merge: while Conflicts remain it answers with the attempt, once every Conflict is resolved and the target has its approvals it creates the merge Revision, which the page then shows with its two parents. The merge engine is the one `examples/merge_demo.py` shows.

## Settings

![Settings](images/settings.png)

Appearance (system, light or dark theme), the connection (where Keeper is, which API token this browser sends, and who Keeper says the caller is), the settings of the Domain (default Branch, creator, search language) and the runtime configuration the app was started with: data source, page size, search limit and Canvas limits. These are build-time values read from environment variables; the app stops at start if a required one is missing. The token field is a development convenience (see [Live mode](../apps/web/README.md#live-mode)).

## Dark theme

![Explorer in the dark theme](images/explorer-home-dark.png)

Colors are CSS variables, so the same screens follow the system theme or a forced light or dark choice kept in local storage.
