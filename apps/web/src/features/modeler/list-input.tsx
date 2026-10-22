import { useState } from 'react';
import { Input } from '@/ui/field';
import { listOfText } from './form-helpers';

/**
 * A comma separated list in a text input. The typed text stays as typed while the field has
 * focus (a trailing comma is not swallowed); the list is written on every change.
 */
export function ListInput({
  label,
  value,
  onChange,
  error,
  hint = 'Comma separated.',
  className,
}: {
  label: string;
  value: string[];
  onChange: (next: string[]) => void;
  error?: string | undefined;
  hint?: string;
  className?: string;
}) {
  const [typed, setTyped] = useState<string | null>(null);
  return (
    <Input
      label={label}
      hint={hint}
      error={error}
      className={className}
      value={typed ?? value.join(', ')}
      onChange={(event) => {
        setTyped(event.target.value);
        onChange(listOfText(event.target.value));
      }}
      onBlur={() => {
        setTyped(null);
      }}
    />
  );
}
