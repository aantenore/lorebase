import { useState } from 'react';
import { Input } from '@/ui/field';
import { scalarFrom, scalarText } from './transform-edits';

/**
 * A scalar argument in a text input. A number, boolean or null stays one while the typed text
 * is still that kind of JSON, even when the field is cleared on the way to a new value.
 */
export function ScalarInput({
  label,
  hint,
  value,
  onChange,
}: {
  label: string;
  hint?: string;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const [typing, setTyping] = useState<{ text: string; origin: unknown } | null>(null);
  return (
    <Input
      label={label}
      {...(hint === undefined ? {} : { hint })}
      value={typing?.text ?? scalarText(value)}
      onChange={(event) => {
        const text = event.target.value;
        const origin = typing === null ? value : typing.origin;
        setTyping({ text, origin });
        onChange(scalarFrom(text, origin));
      }}
      onBlur={() => {
        setTyping(null);
      }}
    />
  );
}
