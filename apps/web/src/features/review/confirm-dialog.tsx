import { common as commonMessages } from '@/messages/common';
import { Button } from '@/ui/button';
import { AUTOFOCUS, Dialog } from '@/ui/dialog';

/**
 * Asks before something that cannot be taken back or that loses work. Focus starts on the
 * cancel button, the least destructive choice, so Enter right after opening loses nothing.
 */
export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel,
  cancelLabel = commonMessages.cancel,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  message: string;
  confirmLabel: string;
  cancelLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <Dialog open={open} onClose={onCancel} title={title}>
      <div className="flex flex-col gap-3">
        <p className="text-sm">{message}</p>
        <div className="flex justify-end gap-2">
          <Button {...AUTOFOCUS} onClick={onCancel}>
            {cancelLabel}
          </Button>
          <Button variant="primary" onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
