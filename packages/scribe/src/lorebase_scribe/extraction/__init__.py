"""Document extraction into reviewable Proposals (design 9.2).

- `parsing`: bytes to text (Markdown and plain text natively, PDF and DOCX with the
  `documents` extra);
- `sandbox`: PDF and DOCX parsed in a child process with time, CPU, memory and size limits and
  no network;
- `text`: structure-aware chunks and sentences with stable offsets;
- `ontology`: what the schema tells the extractors (labels, key patterns, cues, the tier 2
  JSON schema and prompt);
- `matching`: entity resolution of mentions against existing Records;
- `values` and `relations`: rule-based Attribute values and Links (tier 0 and tier 1);
- `tier2`: one structured call per chunk to any chat model, with evidence verification;
- `assemble`: candidates to Proposal items;
- `pipeline`: the `extraction` job.

Modules are imported directly; this package imports nothing so that settings can load early.
"""
