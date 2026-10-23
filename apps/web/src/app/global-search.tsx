import { useNavigate } from '@tanstack/react-router';
import { useEffect, useState, type FormEvent } from 'react';
import { shell as shellMessages } from '@/messages/shell';
import { Button } from '@/ui/button';
import { Dialog } from '@/ui/dialog';
import { Input } from '@/ui/field';
import type { ScopeSearch } from './scope';

/** Global search (Cmd/Ctrl-K). Submitting opens the Explorer search results. */
export function GlobalSearch({ domain, search }: { domain: string; search: ScopeSearch }) {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setOpen(true);
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
    };
  }, []);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const q = query.trim();
    if (q === '') return;
    setOpen(false);
    void navigate({
      to: '/$domain/explorer',
      params: { domain },
      search: { ...search, q },
    });
  };

  return (
    <>
      <Button
        variant="secondary"
        onClick={() => {
          setOpen(true);
        }}
        aria-haspopup="dialog"
      >
        {shellMessages.search.open}
        <kbd className="hidden rounded border border-line px-1 text-xs text-muted sm:inline">
          {shellMessages.search.shortcut}
        </kbd>
      </Button>
      <Dialog
        open={open}
        onClose={() => {
          setOpen(false);
        }}
        title={shellMessages.search.title}
      >
        <form onSubmit={submit} className="flex flex-col gap-3">
          <Input
            label={shellMessages.search.query}
            hideLabel
            placeholder={shellMessages.search.placeholder}
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
            }}
          />
          <Button type="submit" variant="primary">
            {shellMessages.search.submit}
          </Button>
        </form>
      </Dialog>
    </>
  );
}
