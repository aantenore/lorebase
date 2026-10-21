import { renameKind, errorAt } from '@/features/modeler/form-helpers';
import type { KindDocument } from '@/features/modeler/document';
import { useScope } from '@/app/scope';
import { Button } from '@/ui/button';
import { Badge } from '@/ui/layout';
import { AttributesTable } from './attributes-table';
import { ConfidenceBadge, EvidenceDetails } from './confidence-badge';
import { useDraftView } from './draft-context';
import { DecisionButtons, NameInput } from './name-input';
import { idOf } from './proposal-model';

export interface KindCardProps {
  name: string;
  kind: KindDocument;
  open: boolean;
  onToggle: () => void;
}

/** One proposed Kind: key evidence, Attributes with types and samples, and its decision. */
export function KindCard({ name, kind, open, onToggle }: KindCardProps) {
  const { draft, threshold } = useDraftView();
  const scope = useScope();
  const id = idOf(kind);
  const evidence = draft.evidence.get(id);
  const attributes = kind.attributes ?? {};
  const key = kind.primary_key ?? [];
  const accepted = Object.values(attributes).filter(
    (a) => draft.decisions[idOf(a)] === 'accepted',
  ).length;
  const decision = draft.decisions[id];

  return (
    <section aria-label={`Kind ${name}`} className="rounded-md border border-line bg-surface">
      <div className="flex flex-wrap items-start gap-3 p-3">
        <Button
          size="sm"
          aria-expanded={open}
          aria-label={`${open ? 'Collapse' : 'Expand'} Kind ${name}`}
          onClick={onToggle}
        >
          {open ? 'Hide' : 'Show'}
        </Button>
        <div className="w-56">
          <NameInput
            label={`Name of Kind ${name}`}
            value={name}
            taken={Object.keys(draft.document.kinds ?? {})}
            error={errorAt(draft.issues, `kinds/${name}`)}
            onCommit={(next) => {
              draft.edit((document) => {
                renameKind(document, name, next);
              });
            }}
          />
        </div>
        <ConfidenceBadge evidence={evidence} threshold={threshold} />
        {draft.shared.kinds.has(name) && (
          <Badge tone="warning">on {scope.branch}: Attributes merge in</Badge>
        )}
        <span className="text-xs text-muted">
          {Object.keys(attributes).length} Attributes, {accepted} accepted
        </span>
        <div className="ml-auto">
          <DecisionButtons
            what={`Kind ${name}`}
            decision={decision}
            onDecide={(value) => {
              draft.decideKindElement(name, value);
            }}
          />
        </div>
      </div>
      {open && (
        <div className="flex flex-col gap-3 border-t border-line p-3">
          <div className="flex flex-wrap items-baseline gap-x-3 text-sm">
            <span className="font-medium">Primary key</span>
            {key.length > 0 ? (
              <code className="font-mono text-xs">{key.join(', ')}</code>
            ) : (
              <span className="text-muted">none: Records get minted keys</span>
            )}
            <EvidenceDetails evidence={evidence} label="Key evidence" />
          </div>
          <AttributesTable owner={{ type: 'kind', name }} attributes={attributes} keyNames={key} />
        </div>
      )}
    </section>
  );
}
