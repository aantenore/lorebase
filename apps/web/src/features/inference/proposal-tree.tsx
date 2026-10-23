import { useState } from 'react';
import { Button } from '@/ui/button';
import { useDraftView } from './draft-context';
import { KindCard } from './kind-card';
import { idOf } from './proposal-model';
import { RelationsTable } from './relations-table';
import { inference as inferenceMessages } from '@/messages/inference';

/** The proposal as a tree: Kinds with their Attributes, then the Relations between them. */
export function ProposalTree() {
  const { draft } = useDraftView();
  const kinds = Object.entries(draft.document.kinds ?? {});
  const first = kinds[0];
  const [open, setOpen] = useState<ReadonlySet<string>>(
    new Set(first === undefined ? [] : [idOf(first[1])]),
  );

  const toggle = (id: string) => {
    setOpen((current) => {
      const next = new Set(current);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  };

  if (draft.parseError !== null) {
    return (
      <p role="alert" className="text-danger">
        {inferenceMessages.tree.parseError(draft.parseError)}
      </p>
    );
  }
  if (kinds.length === 0) return <p className="text-muted">{inferenceMessages.tree.noKinds}</p>;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex gap-2">
        <Button
          size="sm"
          onClick={() => {
            setOpen(new Set(kinds.map(([, kind]) => idOf(kind))));
          }}
        >
          {inferenceMessages.tree.expandAll}
        </Button>
        <Button
          size="sm"
          onClick={() => {
            setOpen(new Set());
          }}
        >
          {inferenceMessages.tree.collapseAll}
        </Button>
      </div>
      {kinds.map(([name, kind]) => (
        <KindCard
          key={idOf(kind)}
          name={name}
          kind={kind}
          open={open.has(idOf(kind))}
          onToggle={() => {
            toggle(idOf(kind));
          }}
        />
      ))}
      <h3 className="mt-2 text-sm font-semibold">{inferenceMessages.tree.relations}</h3>
      <RelationsTable />
    </div>
  );
}
