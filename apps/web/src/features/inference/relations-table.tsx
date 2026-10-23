import { useScope } from '@/app/scope';
import { errorAt } from '@/features/modeler/form-helpers';
import { Badge } from '@/ui/layout';
import { cn } from '@/ui/cn';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { AttributesTable } from './attributes-table';
import { ConfidenceBadge, EvidenceDetails, percent } from './confidence-badge';
import { useDraftView } from './draft-context';
import { DecisionButtons, NameInput } from './name-input';
import { idOf, inclusionRatios, renameRelation } from './proposal-model';
import { inference as inferenceMessages } from '@/messages/inference';

/** Proposed Relations with cardinality, inclusion ratio and evidence. */
export function RelationsTable() {
  const { draft, threshold } = useDraftView();
  const scope = useScope();
  const relations = Object.entries(draft.document.relations ?? {});
  if (relations.length === 0)
    return <p className="text-sm text-muted">{inferenceMessages.relations.none}</p>;

  return (
    <Table caption={inferenceMessages.relations.caption}>
      <THead>
        <TR>
          <TH>{inferenceMessages.relations.columns.relation}</TH>
          <TH>{inferenceMessages.relations.columns.between}</TH>
          <TH>{inferenceMessages.relations.columns.cardinality}</TH>
          <TH>{inferenceMessages.relations.columns.inclusion}</TH>
          <TH>{inferenceMessages.relations.columns.confidence}</TH>
          <TH>{inferenceMessages.relations.columns.evidence}</TH>
          <TH>{inferenceMessages.relations.columns.decision}</TH>
        </TR>
      </THead>
      <TBody>
        {relations.flatMap(([name, relation]) => {
          const id = idOf(relation);
          const evidence = draft.evidence.get(id);
          const decision = draft.decisions[id];
          const ratios = inclusionRatios(evidence?.reasons ?? []);
          const leftOut = draft.analysis.notApplied.get(id);
          const rows = [
            <TR key={id} className={cn(decision === 'dropped' && 'opacity-50')}>
              <TD className="min-w-48">
                <NameInput
                  label={inferenceMessages.relations.nameOf(name)}
                  value={name}
                  taken={Object.keys(draft.document.relations ?? {})}
                  error={errorAt(draft.issues, `relations/${name}`)}
                  onCommit={(next) => {
                    draft.edit((document) => {
                      renameRelation(document, name, next);
                    });
                  }}
                />
                <p className="mt-1 text-xs text-muted">
                  {inferenceMessages.relations.inverse(relation.inverse_api_name)}
                </p>
                {draft.shared.relations.has(name) && (
                  <Badge tone="warning">{inferenceMessages.relations.kept(scope.branch)}</Badge>
                )}
              </TD>
              <TD>{inferenceMessages.relations.between(relation.from_kind, relation.to_kind)}</TD>
              <TD>
                <code className="font-mono text-xs">{relation.cardinality ?? 'many_to_one'}</code>
              </TD>
              <TD>
                {ratios.length === 0 ? (
                  <span className="text-muted">{inferenceMessages.relations.notApplicable}</span>
                ) : (
                  ratios.map(percent).join(' / ')
                )}
              </TD>
              <TD>
                <ConfidenceBadge evidence={evidence} threshold={threshold} />
              </TD>
              <TD>
                <EvidenceDetails evidence={evidence} />
              </TD>
              <TD>
                <DecisionButtons
                  what={inferenceMessages.relations.decisionTarget(name)}
                  decision={decision}
                  onDecide={(value) => {
                    draft.decideRelationElement(name, value);
                  }}
                />
                {leftOut !== undefined && (
                  <p className="mt-1 text-xs text-warning">
                    {inferenceMessages.decision.leftOut(leftOut)}
                  </p>
                )}
              </TD>
            </TR>,
          ];
          const linkAttributes = relation.attributes ?? {};
          if (Object.keys(linkAttributes).length > 0) {
            rows.push(
              <TR key={`${id}-attributes`}>
                <TD colSpan={7} className="bg-surface-2/40">
                  <p className="mb-1 text-xs font-medium text-muted">
                    {inferenceMessages.relations.linkAttributes(name)}
                  </p>
                  <AttributesTable
                    owner={{ type: 'relation', name }}
                    attributes={linkAttributes}
                    keyNames={[]}
                  />
                </TD>
              </TR>,
            );
          }
          return rows;
        })}
      </TBody>
    </Table>
  );
}
