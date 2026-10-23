import {
  errorAt,
  enumValuesOf,
  renameAttribute,
  typeFromLabel,
  typeLabel,
  typeOptions,
} from '@/features/modeler/form-helpers';
import type { AttributeDocument } from '@/features/modeler/document';
import { Select } from '@/ui/field';
import { Badge } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { cn } from '@/ui/cn';
import { ConfidenceBadge, EvidenceDetails } from './confidence-badge';
import { useDraftView } from './draft-context';
import { DecisionButtons, NameInput } from './name-input';
import { enumSeed, renameLinkAttribute } from './proposal-model';
import { inference as inferenceMessages } from '@/messages/inference';

/** Samples shown per Attribute; the evidence carries a few more. */
const SAMPLES_SHOWN = 3;

export interface AttributesTableProps {
  owner: { type: 'kind' | 'relation'; name: string };
  attributes: Record<string, AttributeDocument>;
  /** Api names of the primary key Attributes (none for a Relation). */
  keyNames: readonly string[];
}

/** The Attributes of a Kind or the link Attributes of a Relation, one reviewable row each. */
export function AttributesTable({ owner, attributes, keyNames }: AttributesTableProps) {
  const { draft, threshold } = useDraftView();
  const kindNames = Object.keys(draft.document.kinds ?? {});
  const section = owner.type === 'kind' ? 'kinds' : 'relations';

  return (
    <Table caption={inferenceMessages.attributes.caption(owner.name)}>
      <THead>
        <TR>
          <TH>{inferenceMessages.attributes.columns.attribute}</TH>
          <TH>{inferenceMessages.attributes.columns.type}</TH>
          <TH>{inferenceMessages.attributes.columns.samples}</TH>
          <TH>{inferenceMessages.attributes.columns.confidence}</TH>
          <TH>{inferenceMessages.attributes.columns.evidence}</TH>
          <TH>{inferenceMessages.attributes.columns.decision}</TH>
        </TR>
      </THead>
      <TBody>
        {Object.entries(attributes).map(([name, attribute]) => {
          const id = attribute.id ?? name;
          const evidence = draft.evidence.get(id);
          const decision = draft.decisions[id];
          const path = `${section}/${owner.name}/attributes/${name}`;
          const label = `${owner.name}.${name}`;
          const isKey = keyNames.includes(name);
          const leftOut = draft.analysis.notApplied.get(id);
          const values = enumValuesOf(attribute.type);
          return (
            <TR key={id} className={cn(decision === 'dropped' && 'opacity-50')}>
              <TD className="min-w-44">
                <NameInput
                  label={inferenceMessages.attributes.nameOf(label)}
                  value={name}
                  taken={Object.keys(attributes)}
                  error={errorAt(draft.issues, path)}
                  onCommit={(next) => {
                    draft.edit((document) => {
                      if (owner.type === 'kind') renameAttribute(document, owner.name, name, next);
                      else renameLinkAttribute(document, owner.name, name, next);
                    });
                  }}
                />
                {isKey && <Badge tone="accent">{inferenceMessages.attributes.key}</Badge>}
              </TD>
              <TD className="min-w-40">
                <Select
                  label={inferenceMessages.attributes.typeOf(label)}
                  hideLabel
                  value={typeLabel(attribute.type)}
                  error={errorAt(draft.issues, `${path}/type`)}
                  options={typeOptions(kindNames, typeLabel(attribute.type)).map((value) => ({
                    value,
                    label: value,
                  }))}
                  onChange={(event) => {
                    const chosen = event.target.value;
                    const seed = values.length > 0 ? values : enumSeed(evidence);
                    draft.edit((document) => {
                      const target =
                        owner.type === 'kind'
                          ? document.kinds?.[owner.name]?.attributes?.[name]
                          : document.relations?.[owner.name]?.attributes?.[name];
                      if (target !== undefined) {
                        target.type = typeFromLabel(chosen, chosen.includes('enum') ? seed : []);
                      }
                    });
                  }}
                />
                {values.length > 0 && (
                  <p className="mt-1 text-xs text-muted">{values.join(', ')}</p>
                )}
              </TD>
              <TD>
                <code className="font-mono text-xs">
                  {(evidence?.samples ?? []).slice(0, SAMPLES_SHOWN).join(', ')}
                </code>
              </TD>
              <TD>
                <ConfidenceBadge evidence={evidence} threshold={threshold} />
              </TD>
              <TD>
                <EvidenceDetails evidence={evidence} />
              </TD>
              <TD>
                <DecisionButtons
                  what={label}
                  decision={decision}
                  lockDrop={isKey ? inferenceMessages.attributes.inKey : undefined}
                  onDecide={(value) => {
                    draft.decideElement(id, value);
                  }}
                />
                {leftOut !== undefined && (
                  <p className="mt-1 text-xs text-warning">
                    {inferenceMessages.decision.leftOut(leftOut)}
                  </p>
                )}
              </TD>
            </TR>
          );
        })}
      </TBody>
    </Table>
  );
}
