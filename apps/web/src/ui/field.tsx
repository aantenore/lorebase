import {
  useId,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react';
import { cn } from './cn';

const CONTROL =
  'h-9 w-full rounded-md border border-line bg-surface px-2 text-sm text-ink placeholder:text-muted disabled:opacity-50';

interface FieldFrameProps {
  label: string;
  hint?: string | undefined;
  error?: string | undefined;
  hideLabel?: boolean | undefined;
  className?: string | undefined;
  children: (ids: { controlId: string; hintId: string | undefined; invalid: boolean }) => ReactNode;
}

function FieldFrame({ label, hint, error, hideLabel, className, children }: FieldFrameProps) {
  const controlId = useId();
  const hintId = hint === undefined && error === undefined ? undefined : `${controlId}-hint`;
  return (
    <div className={cn('flex flex-col gap-1', className)}>
      <label
        htmlFor={controlId}
        className={hideLabel ? 'sr-only' : 'text-xs font-medium text-muted'}
      >
        {label}
      </label>
      {children({ controlId, hintId, invalid: error !== undefined })}
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

export interface InputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'className'> {
  label: string;
  hint?: string;
  /** Why the value is not acceptable; replaces the hint and marks the control invalid. */
  error?: string | undefined;
  hideLabel?: boolean;
  className?: string;
}

/** Labelled text input. The label is always present for assistive technology. */
export function Input({ label, hint, error, hideLabel, className, ...props }: InputProps) {
  return (
    <FieldFrame label={label} hint={hint} error={error} hideLabel={hideLabel} className={className}>
      {({ controlId, hintId, invalid }) => (
        <input
          id={controlId}
          aria-describedby={hintId}
          aria-invalid={invalid || undefined}
          className={CONTROL}
          {...props}
        />
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
  error?: string | undefined;
  hideLabel?: boolean;
  className?: string;
}

/** Labelled native select; keyboard and screen reader behavior come from the platform. */
export function Select({
  label,
  options,
  hint,
  error,
  hideLabel,
  className,
  ...props
}: SelectProps) {
  return (
    <FieldFrame label={label} hint={hint} error={error} hideLabel={hideLabel} className={className}>
      {({ controlId, hintId, invalid }) => (
        <select
          id={controlId}
          aria-describedby={hintId}
          aria-invalid={invalid || undefined}
          className={CONTROL}
          {...props}
        >
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

export interface TextareaProps extends Omit<
  TextareaHTMLAttributes<HTMLTextAreaElement>,
  'className'
> {
  label: string;
  hint?: string;
  /** Why the value is not acceptable; replaces the hint and marks the control invalid. */
  error?: string | undefined;
  hideLabel?: boolean;
  className?: string;
}

/** Labelled multi-line input, for long text and JSON. */
export function Textarea({ label, hint, error, hideLabel, className, ...props }: TextareaProps) {
  return (
    <FieldFrame label={label} hint={hint} error={error} hideLabel={hideLabel} className={className}>
      {({ controlId, hintId, invalid }) => (
        <textarea
          id={controlId}
          aria-describedby={hintId}
          aria-invalid={invalid || undefined}
          className={cn(CONTROL, 'h-auto min-h-20 py-1.5 font-mono')}
          {...props}
        />
      )}
    </FieldFrame>
  );
}
