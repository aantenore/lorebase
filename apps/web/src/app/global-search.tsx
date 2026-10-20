import { useNavigate } from '@tanstack/react-router';
import { useEffect, useState, type FormEvent } from 'react';
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
        Search
        <kbd className="rounded border border-line px-1 text-xs text-muted">Ctrl K</kbd>
      </Button>
      <Dialog
        open={open}
        onClose={() => {
          setOpen(false);
        }}
        title="Search Records"
      >
        <form onSubmit={submit} className="flex flex-col gap-3">
          <Input
            label="Search query"
            hideLabel
            placeholder="Search by name, code or text"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
            }}
          />
          <Button type="submit" variant="primary">
            Search
          </Button>
        </form>
      </Dialog>
    </>
  );
}
