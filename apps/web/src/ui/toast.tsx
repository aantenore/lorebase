import { createContext, use, useCallback, useMemo, useState, type ReactNode } from 'react';
import { cn } from './cn';

export type ToastTone = 'info' | 'success' | 'error';

interface ToastEntry {
  id: number;
  tone: ToastTone;
  message: string;
}

interface ToastApi {
  notify: (message: string, tone?: ToastTone) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

const TONE_CLASS: Record<ToastTone, string> = {
  info: 'border-line',
  success: 'border-success',
  error: 'border-danger',
};

export interface ToastProviderProps {
  children: ReactNode;
  /** How long a toast stays, in milliseconds. */
  durationMs: number;
}

/** Toasts render in a polite live region; errors use an assertive one. */
export function ToastProvider({ children, durationMs }: ToastProviderProps) {
  const [toasts, setToasts] = useState<ToastEntry[]>([]);

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((t) => t.id !== id));
  }, []);

  const notify = useCallback(
    (message: string, tone: ToastTone = 'info') => {
      const id = Date.now() + Math.random();
      setToasts((current) => [...current, { id, tone, message }]);
      window.setTimeout(() => {
        dismiss(id);
      }, durationMs);
    },
    [dismiss, durationMs],
  );

  const value = useMemo(() => ({ notify }), [notify]);

  return (
    <ToastContext value={value}>
      {children}
      <div className="pointer-events-none fixed right-4 bottom-4 z-50 flex w-80 flex-col gap-2">
        <div role="status" aria-live="polite" className="flex flex-col gap-2">
          {toasts
            .filter((t) => t.tone !== 'error')
            .map((t) => (
              <ToastCard key={t.id} toast={t} onDismiss={dismiss} />
            ))}
        </div>
        <div role="alert" className="flex flex-col gap-2">
          {toasts
            .filter((t) => t.tone === 'error')
            .map((t) => (
              <ToastCard key={t.id} toast={t} onDismiss={dismiss} />
            ))}
        </div>
      </div>
    </ToastContext>
  );
}

function ToastCard({ toast, onDismiss }: { toast: ToastEntry; onDismiss: (id: number) => void }) {
  return (
    <div
      className={cn(
        'pointer-events-auto flex items-start justify-between gap-2 rounded-md border bg-surface p-3 text-sm shadow-lg',
        TONE_CLASS[toast.tone],
      )}
    >
      <span>{toast.message}</span>
      <button
        type="button"
        className="text-muted hover:text-ink"
        aria-label="Dismiss notification"
        onClick={() => {
          onDismiss(toast.id);
        }}
      >
        x
      </button>
    </div>
  );
}

export function useToast(): ToastApi {
  const api = use(ToastContext);
  if (api === null) throw new Error('useToast must be used inside ToastProvider.');
  return api;
}
