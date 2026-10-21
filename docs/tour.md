# Visual tour

Screenshots of the Lorebase web app (`apps/web`), taken at 1440x900 in the light theme.

**Live and mock.** Except `documents.png` and `review-items.png`, which come from the mock (a live run does not load documents into Keeper), the pictures below come from the app in live mode against a running Keeper: Postgres and Keeper from `deploy/compose`, the `examples/supply` sample loaded through the REST API (30 Suppliers, 40 Parts, 20 Plants and their Links), and a Proposal with two Conflicts left open. They were taken with `pnpm screenshots --live`. The same screens run on an in-memory mock of the Keeper API when `VITE_API_MODE=mock` (two sample Domains, no backend), which is what the unit tests and `pnpm screenshots` without `--live` use. The flows behind the pictures are exercised against a real Keeper by the Playwright suite in `apps/web/e2e` (browse a Supplier and its Canvas, create a Branch, change the schema through a Proposal, resolve Conflicts and merge, Settings), which CI runs on every change; see [Status](../README.md#status-and-roadmap) for what is still planned. Not everything is served yet: Keeper has no per-Record history list (the Record page lists the writes `explain` knows), no derived values and no extraction items, and the screens say so where it matters. The Start from data and Sources pictures (sample runs) and the animation below were recorded in mock mode.

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

### Start from data

![Modeler, Start from data](images/modeler-start.png)

The wizard for a Domain that starts from tables, not from a blank schema. Upload CSV or TSV files (drag and drop, with the file, size and suffix caps of the configuration) or name a Postgres source by its allowlisted secret reference, run the inference, and review what it proposes: Kinds with a confidence badge and the evidence for their primary key, Attributes with their type, sample values and the reasons behind them, Relations with cardinality and inclusion ratio. Rename, change a type, accept or drop each element, or accept everything at or above a confidence threshold; the YAML on the right follows every edit, and the issues the inference found are listed below. "Apply as schema change" lays the accepted elements over the schema of the Branch and commits them through the same change set panel as the schema editor, or, on a protected Branch such as `main`, creates a Branch and opens a Proposal. The picture shows the twelve CSV files of `examples/supply` after "Accept all at or above 90%": 61 of 63 elements accepted, the two junction Relations read with 85% confidence left to the reviewer. Mock: the inference answer is the recorded output of the real engine on those files (the gold test of `docs/inference.md`), narrowed to the tables you upload; the uploaded bytes are not read.

## Sources

![Sources](images/sources.png)

The Sources of the schema with their connection reference, mapping summary, sync mode and merge policy, schedule, last run and its counts, a "Sync now" button, the run history with the error of a failed run and a link to the Proposal a run opened (run 6 opened Proposal 40), and the `sources` section of the schema as a YAML editor with validation and a change set. Mock: runs are sample data and a manual sync finds nothing new. Live: runs come from Keeper's Source routes and "Sync now" queues a job for Scribe. The mapping editor is planned.

## Documents

![Documents](images/documents.png)

Where contracts, notices and reports enter a Domain to be read into Proposals (`docs/extraction.md`). A drop zone and a picker take the files, with the caps of the configuration (documents per drop, bytes per document, suffixes) stated and enforced, a switch for "Extract after storing" and the extraction tiers, and a form takes a URL for the hosts Keeper allows. The library lists each document with its type, size, who added it, the status of its latest extraction job (the picture shows four succeeded jobs that opened Proposal 44, one that opened Proposal 42, and a scanned PDF whose job failed) and a link to the Proposal; it follows a queued or running job until it ends. A document opens in a viewer with its jobs (model, prompt version, tokens, cost) and its parsed text, with the evidence of the Proposal's items highlighted and a list of those items beside it. Mock: the four documents are the texts of `examples/supply/documents`; a new upload is stored but extracts nothing. Live: the Keeper has no route for the parsed text yet, so the viewer shows the evidence passages instead.

## Review

### Proposal list

![Review, Proposal list](images/review-list.png)

The Proposals of the Domain with origin (manual or extraction), the Branches they merge, status and the number of unresolved Conflicts, filterable by status. The Proposal page also has the checks Keeper runs, the reviews with where the approvals stand (a protected target wants approvals before it takes a merge) and a form to add one.

### Extraction items

![Review, extraction items](images/review-items.png)

The Items tab of an extraction Proposal. Proposal 44 holds 13 items from the four supply documents: new Records, Attribute values and Links, grouped by type, each with a confidence bar, how it matched the Records of `main` (`matched`, `new`, `ambiguous`), its quote and a link to the quote in the document viewer. Filters narrow the table by status, Kind or Relation, type and confidence range; accept, reject and back to pending are staged per item (buttons or `a`, `r`, `u`, with arrow keys or `j` and `k` to move) or in bulk for what the filter shows or for the pending items above or below a threshold, and "Apply decisions" sends them in one request. Keeper checks the accepted set as a whole: accepting the Contract C-3025 without its `with_supplier` Link is a 422 whose findings are shown next to the staged decisions, and accepting both rebuilds the extraction Branch with exactly the accepted items, reporting the changes, the Revision and the accepted items that could not be applied. The selected item (right) shows its evidence, a picker for the candidates of an ambiguous match (here the Dresden plant, chosen between two) and inline edits of its values or Link ends, checked against the schema. The picture has the Contract and its Link staged and the ambiguous Link selected.

### Merge attempt and Conflict resolver

![Review, Conflict resolver](images/review-conflict.png)

Two Branches changed the same Supplier Attributes (the risk score of S-1001 and the tier of S-1002), and the Proposal that merges the second ends up with two Conflicts of kind `DV1`. Conflicts are grouped by kind, filterable, and one is selected in the URL; they are named by Kind, key and Attribute, not by the ids Keeper uses. The resolver shows base, ours and theirs side by side and offers only the resolutions that fit the Conflict's kind (take ours, take theirs, revert to base, custom value); grouped Conflicts take one value per member. "Apply resolutions" asks Keeper to merge: while Conflicts remain it answers with the attempt, once every Conflict is resolved and the target has its approvals it creates the merge Revision, which the page then shows with its two parents. The merge engine is the one `examples/merge_demo.py` shows.

## Settings

![Settings](images/settings.png)

Appearance (system, light or dark theme), the connection (where Keeper is, which API token this browser sends, and who Keeper says the caller is), the settings of the Domain (default Branch, creator, search language) and the runtime configuration the app was started with: data source, page size, search limit and Canvas limits. These are build-time values read from environment variables; the app stops at start if a required one is missing. The token field is a development convenience (see [Live mode](../apps/web/README.md#live-mode)).

Three more tabs hold the Admin basics (not pictured): API keys (create with a role and a lifetime, the secret is shown once, revoke after a confirmation, last use and status), principals and the grants of the Domain (read only), and the audit log with actor, action and text filters over the loaded entries and paging for older ones. Mock: keys, grants and audit entries live in memory for the session.

## Dark theme

![Explorer in the dark theme](images/explorer-home-dark.png)

Colors are CSS variables, so the same screens follow the system theme or a forced light or dark choice kept in local storage.
