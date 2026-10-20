# Visual tour

Screenshots of the Lorebase web app (`apps/web`), taken from the `supply` sample Domain at 1440x900 in the light theme.

**Mock versus live.** Every picture below comes from mock mode: the app talks to an in-memory implementation of the Keeper API with two sample Domains (`supply` and `hr`), so no backend is involved. The Keeper REST API now covers Domains, branches, schema and schema change sets, Operations, Proposals with merge and Conflict resolution, Records, Links, query, traversal, neighborhood, search, log, Revisions and diff, and the app has an HTTP adapter (`live` mode) for it, but the screens, Review included, have not been exercised against a running Keeper (see [Status](../README.md#status-and-roadmap)). Data you see is sample data, and changes made in the app live only for the browser session.

![Explorer, a Record, the Canvas and the Review resolver in one pass](images/tour.gif)

The images are regenerated with `pnpm screenshots` in `apps/web`, and the animation above with `pnpm screenshots flow`; see [apps/web/README.md](../apps/web/README.md#screenshots).

## Explorer

### Domain home

![Explorer, Domain home](images/explorer-home.png)

The entry point of a Domain on a Branch. It offers search over Records with a Kind filter, the Kinds of the Domain, the latest Revisions of the Branch (with their origin, such as `source_sync` or `schema_change`) and the Proposals waiting for review. The Domain and Branch switchers and the head Revision in the top bar apply to every screen. Mock: all content is sample data. The screen reads only through the public API interface, so the same code runs against Keeper once live mode is verified. Screens listed under "planned" at the bottom are placeholders.

### Kind table

![Explorer, Kind table](images/explorer-kind.png)

The Records of one Kind as a table, with a column per Attribute, sorting and cursor paging. Each row opens the Record page. Mock: the six Suppliers of the sample Domain, served by the mock query engine; paging and sorting follow the same query contract the HTTP adapter uses.

### Record page

![Explorer, Record page](images/explorer-record.png)

One Record with its Attribute values, the Relations it takes part in (Links in both directions, each one navigable) and its Lineage: for every Attribute, where the value came from, who wrote it and in which Revision. "Open in Canvas" jumps to the graph around the Record. Mock: Lineage and Links are seeded sample data.

## Canvas

### Graph explorer

![Canvas, graph explorer](images/canvas-graph.png)

The neighborhood of a few Records drawn as a graph: nodes are Records coloured by Kind, edges are Links, and a click shows the details of the selected node. Depth and node cap are configuration values. The drawing uses Sigma.js, which needs WebGL 2; where it is missing, the screen shows a message and a list view of the same nodes and edges, which is also available under the graph. The image above was captured with the WebGL canvas rendering (software GL in headless Chromium). Mock: the neighborhood is computed by the mock; a worker-based layout for large graphs is planned.

### Schema graph

![Canvas, schema graph](images/canvas-schema.png)

The schema of the Branch as a graph: Kinds as nodes, Relations as edges, with the same selection panel and an "Open in Modeler" link. It is a quick way to see how the Domain is shaped before editing it. Mock: the schema of the sample Domain. The third tab of the Canvas, the diff overlay, is not covered here.

## Modeler

### Schema overview

![Modeler, list view](images/modeler-list.png)

The schema of the current Branch as lists: Kinds, Attributes, Relations, Traits, Operations, Derivations and Sources, with counts. On a protected Branch such as `main` the banner says that schema changes go through another Branch and a Proposal. Mock: the schema is the sample Domain's; Operation, Derivation and Source mapping editors are marked "Planned".

### Edit schema

![Modeler, schema editor](images/modeler-edit.png)

The schema as one document in two synchronized views: a form for Kinds, Relations and Traits and a YAML editor (CodeMirror) for everything, including Operations, Derivations and Sources. Validation issues point at the field and the YAML line; the change set summary shows the commands and the data migrations a dry run reports. On a protected Branch "Open Proposal" creates a Branch and a Proposal instead of committing. Mock: validation, migrations and commits run in the mock with the same rules as the Keeper design. Keeper now has a schema change endpoint, but this editor has not been run against it yet.

## Review

### Proposal list

![Review, Proposal list](images/review-list.png)

The Proposals of the Domain with origin (manual or extraction), the Branches they merge, status and the number of unresolved Conflicts, filterable by status. Mock: three open sample Proposals (#41, #42 and #43). The extraction items screen is planned.

### Merge attempt and Conflict resolver

![Review, Conflict resolver on Proposal 41](images/review-conflict.png)

Proposal 41 merges `model/risk` into `main` and ends up with six Conflicts of six kinds, plus one that a merge policy already resolved. Conflicts are grouped by kind, filterable, and one is selected in the URL. The resolver shows base, ours and theirs side by side and offers only the resolutions that fit the Conflict's kind (take ours, take theirs, revert to base, custom value); grouped Conflicts take one value per member. "Apply resolutions" attempts the merge and, once every Conflict is resolved, creates the merge Revision. Mock: the Conflicts are seeded, not computed from the Branches, and the merge applies resolutions to a copy of the source state; Keeper has no Proposal or merge endpoints yet, so this screen cannot run against it today. The merge engine itself is complete and shown by `examples/merge_demo.py`.

## Settings

![Settings](images/settings.png)

Appearance (system, light or dark theme), the settings of the Domain (default Branch, creator, search language) and the runtime configuration the app was started with: data source, page size, search limit and Canvas limits. These are build-time values read from environment variables; the app stops at start if a required one is missing. Mock: the data source reads "mock".

## Dark theme

![Explorer in the dark theme](images/explorer-home-dark.png)

Colors are CSS variables, so the same screens follow the system theme or a forced light or dark choice kept in local storage.
