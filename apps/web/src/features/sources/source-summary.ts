import type { Schema, Source } from '@/api/types';
import type { SourceRun } from '@/api/sources-types';
import { sources as sourcesMessages } from '@/messages/sources';

/** One line on what a Source maps: Attributes and Relations into its Kind, and the key columns. */
export function mappingSummary(source: Source, schema: Schema): string {
  const kind =
    schema.kinds.find((k) => k.id === source.target_kind)?.api_name ?? source.target_kind;
  const parts = [
    sourcesMessages.summary.attributesInto(Object.keys(source.attribute_mapping).length, kind),
  ];
  const relations = Object.keys(source.relation_mapping).length;
  if (relations > 0) parts.push(sourcesMessages.summary.relations(relations));
  if (source.key_mapping.length > 0)
    parts.push(sourcesMessages.summary.key(source.key_mapping.join(', ')));
  return parts.join(', ');
}

/** Sync mode and merge policy, as the Source states them. */
export function policyLabel(source: Source): string {
  return `${source.sync_mode}, ${source.target_branch_policy.merge_policy}`;
}

/** The newest run of a Source; runs are listed newest first. */
export function lastRunOf(runs: readonly SourceRun[], source: string): SourceRun | undefined {
  return runs.find((run) => run.source === source);
}

/** `+1 ~5 -0`: rows added, updated and deleted by a run. */
export function changesLabel(run: SourceRun): string {
  const { added, updated, deleted } = run.changes;
  return `+${added.toString()} ~${updated.toString()} -${deleted.toString()}`;
}
