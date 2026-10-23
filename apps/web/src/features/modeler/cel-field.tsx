import { lazy, Suspense, useId } from 'react';
import { modeler as modelerMessages } from '@/messages/modeler';
import { cn } from '@/ui/cn';

// CodeMirror loads with the first expression field, in the chunk the YAML editor shares.
const CelEditor = lazy(() => import('./cel-editor'));

export interface CelFieldProps {
  label: string;
  value: string;
  onChange: (text: string) => void;
  hint?: string | undefined;
  /** Why the expression is not acceptable, from the local rules or from Keeper's type check. */
  error?: string | undefined;
  placeholder?: string | undefined;
  className?: string | undefined;
}

/** A labelled CEL expression field with its error or hint underneath. */
export function CelField({
  label,
  value,
  onChange,
  hint,
  error,
  placeholder,
  className,
}: CelFieldProps) {
  const id = useId();
  const hintId = hint === undefined && error === undefined ? undefined : `${id}-hint`;
  return (
    <div className={cn('flex min-w-0 flex-col gap-1', className)} role="group" aria-label={label}>
      <span className="text-xs font-medium text-muted" aria-hidden="true">
        {label}
      </span>
      <Suspense
        fallback={
          <p role="status" className="text-xs text-muted">
            {modelerMessages.expressions.loading}
          </p>
        }
      >
        <CelEditor
          value={value}
          onChange={onChange}
          label={label}
          describedBy={hintId}
          invalid={error !== undefined}
          placeholder={placeholder}
        />
      </Suspense>
      {error !== undefined ? (
        <p id={hintId} className="text-xs text-danger">
          {error}
        </p>
      ) : (
        hint !== undefined && (
          <p id={hintId} className="text-xs text-muted">
            {hint}
          </p>
        )
      )}
    </div>
  );
}
