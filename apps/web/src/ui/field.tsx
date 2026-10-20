import { useId, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes } from 'react';
import { cn } from './cn';

const CONTROL =
  'h-9 w-full rounded-md border border-line bg-surface px-2 text-sm text-ink placeholder:text-muted disabled:opacity-50';

interface FieldFrameProps {
  label: string;
  hint?: string | undefined;
  hideLabel?: boolean | undefined;
  className?: string | undefined;
  children: (ids: { controlId: string; hintId: string | undefined }) => ReactNode;
}

function FieldFrame({ label, hint, hideLabel, className, children }: FieldFrameProps) {
  const controlId = useId();
  const hintId = hint === undefined ? undefined : `${controlId}-hint`;
  return (
    <div className={cn('flex flex-col gap-1', className)}>
      <label
        htmlFor={controlId}
        className={hideLabel ? 'sr-only' : 'text-xs font-medium text-muted'}
      >
        {label}
      </label>
      {children({ controlId, hintId })}
      {hint !== undefined && (
        <p id={hintId} className="text-xs text-muted">
          {hint}
        </p>
      )}
    </div>
  );
}

export interface InputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'className'> {
  label: string;
  hint?: string;
  hideLabel?: boolean;
  className?: string;
}

/** Labelled text input. The label is always present for assistive technology. */
export function Input({ label, hint, hideLabel, className, ...props }: InputProps) {
  return (
    <FieldFrame label={label} hint={hint} hideLabel={hideLabel} className={className}>
      {({ controlId, hintId }) => (
        <input id={controlId} aria-describedby={hintId} className={CONTROL} {...props} />
      )}
    </FieldFrame>
  );
}

export interface SelectOption {
  value: string;
  label: string;
}

export interface SelectProps extends Omit<SelectHTMLAttributes<HTMLSelectElement>, 'className'> {
  label: string;
  options: SelectOption[];
  hint?: string;
  hideLabel?: boolean;
  className?: string;
}

/** Labelled native select; keyboard and screen reader behavior come from the platform. */
export function Select({ label, options, hint, hideLabel, className, ...props }: SelectProps) {
  return (
    <FieldFrame label={label} hint={hint} hideLabel={hideLabel} className={className}>
      {({ controlId, hintId }) => (
        <select id={controlId} aria-describedby={hintId} className={CONTROL} {...props}>
          {options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      )}
    </FieldFrame>
  );
}
