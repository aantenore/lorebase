"""Server instructions and the `lorebase_guide` prompt template (no model calls)."""

from __future__ import annotations

INSTRUCTIONS = """\
Lorebase is a versioned knowledge graph: typed Records (refs `Kind/key`) linked by Relations,
on branches of immutable Revisions. Workflow:
1. Orient: lorebase_context (records or a question) or lorebase_schema.
2. Read narrowly: lorebase_get, lorebase_query, lorebase_search, lorebase_neighborhood;
   lorebase_explain for where a value came from; lorebase_changes / lorebase_diff for history.
3. Write only with lorebase_execute: dry_run first (the default), then dry_run=false with
   expected_head. Some Operations need review: pass task and intent and Keeper opens the
   Proposal on a task branch for you.
Outputs carry the Revision they were read at and say when they were cut."""

GUIDE = """\
You are working with the Lorebase Domain {domain}.

How to use Lorebase:
- Records are addressed as `Kind/key` (for example `Supplier/S-1001`); use api names exactly as
  lorebase_schema prints them.
- Start with lorebase_context: pass the Records the task is about, or a short question; it
  returns those Records, their links, nearby Records and the schema of the Kinds involved,
  within a token budget.
- Then read only what you need: lorebase_get for one Record, lorebase_query for filtered lists
  (JSON DSL), lorebase_search to find Records by words, lorebase_neighborhood for links.
- Facts carry provenance: lorebase_explain shows who or what set each value and when.
- History: lorebase_changes lists Revisions since one you saw; lorebase_diff compares two.
- Writes go only through governed Operations with lorebase_execute. Always dry-run first
  (dry_run is true by default), check edits and violations, then commit with dry_run=false and
  expected_head set to the head the dry run reported. A `review-required` or
  `protected-branch` error means the change must go through a Proposal: repeat the call with
  `task` and `intent` and send an `idempotency_key`; Keeper opens the Proposal on a task branch.
- Errors start with `[code]` and end with `next:`; follow that step instead of retrying blindly.
- Outputs are cut at a size limit and say so; narrow the request or follow `next_cursor`.
{task}"""


def guide(domain: str | None, task: str | None) -> str:
    """The guide text for a Domain and, optionally, the task at hand."""
    return GUIDE.format(
        domain=domain or "configured for this server",
        task=f"\nTask: {task}" if task else "",
    )
