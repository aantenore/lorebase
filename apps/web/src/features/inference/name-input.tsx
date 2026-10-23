import { useState } from 'react';
import { Button } from '@/ui/button';
import { Input } from '@/ui/field';
import type { Decision } from './proposal-model';
import { inference as inferenceMessages } from '@/messages/inference';

export interface NameInputProps {
  label: string;
  value: string;
  /** Names a sibling already uses. */
  taken: readonly string[];
  /** Why the document rejects the current name (schema rules), if it does. */
  error?: string | undefined;
  onCommit: (name: string) => void;
}

/**
 * A name edited in place. The rename is applied when the field loses focus or on Enter, so
 * the references to the element are rewritten once, not on every keystroke. An empty name or
 * one a sibling uses is refused and the previous name returns.
 */
export function NameInput({ label, value, taken, error, onCommit }: NameInputProps) {
  const [draft, setDraft] = useState(value);
  const [seen, setSeen] = useState(value);
  const [refusal, setRefusal] = useState<string | undefined>();
  if (value !== seen) {
    setSeen(value);
    setDraft(value);
  }

  const commit = () => {
    const name = draft.trim();
    if (name === value) {
      setDraft(value);
      setRefusal(undefined);
    } else if (name === '' || taken.includes(name)) {
      setRefusal(
        name === ''
          ? inferenceMessages.decision.nameRequired
          : inferenceMessages.decision.nameTaken(name),
      );
      setDraft(value);
    } else {
      setRefusal(undefined);
      onCommit(name);
    }
  };

  return (
    <Input
      label={label}
      hideLabel
      value={draft}
      error={refusal ?? error}
      onChange={(event) => {
        setDraft(event.target.value);
      }}
      onBlur={commit}
      onKeyDown={(event) => {
        if (event.key === 'Enter') commit();
      }}
    />
  );
}

/** Accept and Drop for one element; pressing the active one clears the decision. */
export function DecisionButtons({
  what,
  decision,
  onDecide,
  lockDrop,
}: {
  /** Name of the element, for the accessible labels: "Accept Asset.tier". */
  what: string;
  decision: Decision | undefined;
  onDecide: (value: Decision | null) => void;
  /** Why the element cannot be dropped (a primary key), if it cannot. */
  lockDrop?: string;
}) {
  return (
    <div className="flex gap-1" role="group" aria-label={inferenceMessages.decision.group(what)}>
      <Button
        size="sm"
        variant={decision === 'accepted' ? 'primary' : 'secondary'}
        aria-pressed={decision === 'accepted'}
        aria-label={inferenceMessages.decision.acceptLabel(what)}
        onClick={() => {
          onDecide(decision === 'accepted' ? null : 'accepted');
        }}
      >
        {inferenceMessages.decision.accept}
      </Button>
      <Button
        size="sm"
        variant={decision === 'dropped' ? 'danger' : 'secondary'}
        aria-pressed={decision === 'dropped'}
        aria-label={inferenceMessages.decision.dropLabel(what)}
        disabled={lockDrop !== undefined}
        title={lockDrop}
        onClick={() => {
          onDecide(decision === 'dropped' ? null : 'dropped');
        }}
      >
        {inferenceMessages.decision.drop}
      </Button>
    </div>
  );
}
