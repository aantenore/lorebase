# Merging a knowledge base: what happens when analysts and an ERP disagree

*Draft for a technical blog post, about 1,200 words. The output below is real, from `uv run python examples/merge_demo.py`. Link to [docs/why.md](../why.md) for the argument and to the repository for the code.*

Suppose a supplier risk score lives in two places. Analysts maintain it by hand. A nightly feed from the ERP also writes it. Both are right about something, and they overlap on a Tuesday. In most systems one of the two writes wins and the other disappears. Nobody notices until an agent that reads this data recommends the wrong supplier.

Lorebase is an open-source ontology platform built around the idea that this should be a merge conflict. It stores a typed knowledge graph in Postgres, versions schema and data together with branches and immutable Revisions, and exposes it to AI agents through an API that never calls a language model. This post walks through one merge, because the merge is where the design shows.

## The setup

The demo uses a small supply-chain Domain called `supply`: Suppliers, Parts, Plants and the Relations between them, 30 Suppliers, 40 Parts and 48 Links on `main`. The schema is a YAML file, so it can be reviewed like code. Two branches start from the same Revision, `7ad4a553`.

`model/risk` belongs to the analysts. They change the schema: `risk_band` becomes a required attribute, and they backfill it. They also set Supplier `S-1001` to a risk score of 80, set `S-1002` to 55, delete Part `P-10003` because it is retired, and add a new Supplier `S-1031`.

`source/erp` is the nightly feed. It changes data only: `S-1001` gets a risk score of 45, `S-1002` a new city, `P-10003` a new unit cost, and it adds three Suppliers, among them `S-1031`, which the analysts also added.

## Merge one: no conflict

Merging `model/risk` into `main` is trivial, because `main` has not moved:

```text
== 4. merge model/risk into main
fast_forward: 34 changes in 4 partitions
```

The branch head simply becomes the new head of `main`. Nothing to decide.

## Merge two: five conflicts

Now `source/erp` is merged into a `main` that already contains the analysts' work. Both sides moved since the common ancestor, so the engine does a three-way merge. It diffs the base against each side and combines the results in a fixed order. The schema is merged first. Then each Record is merged at attribute level: if only one side changed an attribute, that change wins; if both changed it to the same value, fine; otherwise it is a conflict. Finally the merged data is checked against the merged schema, which is how the last kind of conflict below appears.

```text
== 5. merge source/erp into main
merge base 7ad4a553: 5 Conflicts

DV1 ATTRIBUTE_VALUE (1)
  where            attribute   base  ours  theirs  choices
  -----            ---------   ----  ----  ------  -------
  Supplier S-1001  risk_score  32    80    45      ours/theirs/base/custom

DV2 DELETE_MODIFY (1)
  where         attribute  base      ours      theirs    choices
  -----         ---------  ----      ----      ------    -------
  Part P-10003  -          {record}  (absent)  {record}  ours/theirs/base/custom

DV3 ADD_ADD (2)
  where            attribute   base      ours         theirs                       choices
  -----            ---------   ----      ----         ------                       -------
  Supplier S-1031  legal_name  (absent)  Norvik GmbH  Norvik Fasteners Sp. z o.o.  ours/theirs/base/custom
  Supplier S-1031  risk_score  (absent)  35           50                           ours/theirs/base/custom

XS4 MISSING_VALUE (1)
  where                        attribute  base  ours  theirs  choices
  -----                        ---------  ----  ----  ------  -------
  Supplier x2: S-1032, S-1033  risk_band                      custom/relax
```

Each conflict is a different animal, and each offers only the resolutions that make sense for it.

**DV1, attribute value.** `S-1001` had a score of 32. The analysts moved it to 80, the ERP to 45. Both are plausible; a person has to choose. The choices are ours, theirs, the base value or a custom one.

**DV2, delete against modify.** The analysts deleted `P-10003`; the feed updated its unit cost. Keeping the deletion would discard a real update, and keeping the part would resurrect something retired. The engine will not guess.

**DV3, add against add.** Both sides created `S-1031`, with different names and scores. The merge treats the base as empty and conflicts on each attribute that differs.

**XS4, missing value.** This is the one that needs the schema and the data to be merged together. The analysts made `risk_band` required. The feed, working from the old schema, created `S-1032` and `S-1033` without one. Neither side did anything wrong, and neither change conflicts with the other when looked at alone. Together they produce records that violate the merged schema. The resolutions are to provide a value or to relax the requirement.

A tool that merges data and schema separately would have committed those two Suppliers and left the knowledge base in a state that its own schema forbids. This is the failure we built the engine to prevent. The other cross-cutting checks follow the same pattern: type coercion when a type changes under new data, cardinality, required Relations, uniqueness, dangling Links, constraints, Trait conformance. The [full taxonomy](../design.md#69-conflict-taxonomy) has the list.

## Resolving

The demo resolves all five, and each resolution carries its reason:

```text
== 6. resolutions
  DV1 Supplier S-1001: custom, analysts say 80, ERP says 45
  DV3 Supplier S-1031: ours, keep the analysts' Record
  DV3 Supplier S-1031: ours, keep the analysts' Record
  DV2 Part P-10003: ours, the part is retired, keep it gone
  XS4 Supplier x2: S-1032, S-1033: custom, unscored until reviewed
```

Then the merge is attempted again:

```text
== 7. merge again
merged: Revision 2cacd1bb, parents 60f665d3, 4d5440a3
4 changes in 1 partitions, counts per Kind:
  kind      created  updated  deleted
  ----      -------  -------  -------
  Supplier  2        2        0
validation: 72 Records against the merged schema, 0 issues
```

The merge Revision has two parents, so the history shows both lines of work. Provenance on each change is kept, so `S-1001` can still be traced to whoever and whatever produced each value. The final check validates every Record against the merged schema and finds nothing wrong.

## How it works underneath

Three decisions make this cheap enough to be practical.

Revisions are content-addressed. Every Record, Link and schema element is hashed, and the Records live in a canonical hash-array-mapped tree, split into partitions. The same content gives the same root hash regardless of insertion order, so two Revisions that share most of their content share most of their nodes, and diffing two Revisions only walks the subtrees whose hashes differ.

Reads do not go through the tree. A materializer keeps a relational projection of each branch head in Postgres, with indexes, and every read is SQL against that. History and time travel use the tree. The projection can be rebuilt from the tree at any time, which is what the verification step checks.

The read path has no model in it. This is enforced by the build: the API service is forbidden from importing the inference package. A read is a query, so it is deterministic and takes milliseconds, which matters for an agent that issues dozens of them to answer one question.

## What exists, and what does not

Lorebase is pre-alpha. The engine you just watched, the Postgres store, and a REST API with Domains, branches, schema change sets, governed Operations, Records, a query DSL, traversal, neighborhood, search, log and diff are built and tested. Authentication supports API keys and OIDC, and reads can be served from Postgres replicas behind a freshness guard.

Not built: Proposals and merge over REST (the engine is done, the endpoints are next), the CLI and Python client, the MCP server, Source ingestion and document extraction. The web app, including the conflict resolver you can see in the GIF in the README, runs on a mock API. No benchmark is published.

## Try it

```bash
git clone https://github.com/aantenore/lorebase.git && cd lorebase
uv sync --all-packages
uv run python examples/merge_demo.py
```

It needs no services and takes a couple of seconds. If you manage knowledge that agents depend on, I would like to hear which conflicts you would expect to see that this does not cover.
