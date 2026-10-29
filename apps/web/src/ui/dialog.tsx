import { useEffect, useId, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { common as commonMessages } from '@/messages/common';
import { Button } from './button';

export interface DialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
}

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Marks the control a dialog focuses when it opens, in place of its first one. */
export const AUTOFOCUS = { 'data-autofocus': true } as const;

/**
 * Modal dialog: labelled by its title, closes on Escape and backdrop click, keeps
 * Tab inside, moves focus in on open and restores it on close.
 *
 * On open, focus goes (WAI-ARIA dialog pattern) to the control marked with `AUTOFOCUS`, else
 * to the first control of the content, else to the title: never to the Close button, which
 * stays reachable with Tab and Escape.
 */
export function Dialog({ open, onClose, title, children }: DialogProps) {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const titleRef = useRef<HTMLHeadingElement>(null);
  // The latest handler is read through a ref so re-renders do not re-run the focus logic.
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const panel = panelRef.current;
    const body = bodyRef.current;
    const initial =
      body?.querySelector<HTMLElement>('[data-autofocus]') ??
      body?.querySelector<HTMLElement>(FOCUSABLE) ??
      titleRef.current;
    initial?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab' || panel === null) return;
      const items = [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)];
      const head = items[0];
      const tail = items[items.length - 1];
      if (head === undefined || tail === undefined) return;
      if (event.shiftKey && document.activeElement === head) {
        event.preventDefault();
        tail.focus();
      } else if (!event.shiftKey && document.activeElement === tail) {
        event.preventDefault();
        head.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      previous?.focus();
    };
  }, [open]);

  if (!open) return null;

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 p-4 pt-[12vh]"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="w-full max-w-lg rounded-lg border border-line bg-surface p-4 shadow-xl"
      >
        <div className="mb-3 flex items-center justify-between gap-2">
          <h2 id={titleId} ref={titleRef} tabIndex={-1} className="text-base font-semibold">
            {title}
          </h2>
          <Button
            variant="ghost"
            size="sm"
            onClick={onClose}
            aria-label={commonMessages.closeDialog}
          >
            {commonMessages.close}
          </Button>
        </div>
        <div ref={bodyRef}>{children}</div>
      </div>
    </div>,
    document.body,
  );
}
