import { useState } from 'react';
import { Input } from '@/ui/field';

/** A key name; the rename applies while the typed name is free, as typed. */
export function KeyInput({
  label,
  name,
  taken,
  onRename,
}: {
  label: string;
  name: string;
  taken: (name: string) => boolean;
  onRename: (name: string) => void;
}) {
  const [typed, setTyped] = useState<string | null>(null);
  const problem = typed !== null && taken(typed) ? `${typed} is set already.` : undefined;
  return (
    <Input
      label={label}
      value={typed ?? name}
      error={problem}
      onChange={(event) => {
        const next = event.target.value;
        setTyped(next);
        if (next.trim() !== '' && !taken(next)) onRename(next);
      }}
      onBlur={() => {
        setTyped(null);
      }}
    />
  );
}
