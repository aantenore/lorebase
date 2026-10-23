import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from '@tanstack/react-router';
import { useState } from 'react';
import { useKeeperApi } from '@/api/provider';
import { common as commonMessages } from '@/messages/common';
import { shell as shellMessages } from '@/messages/shell';
import { Button } from '@/ui/button';
import { Dialog } from '@/ui/dialog';
import { Input } from '@/ui/field';
import { useToast } from '@/ui/toast';
import type { ScopeSearch } from './scope';

export interface NewBranchProps {
  domain: string;
  /** The Branch the new one starts from: the one on screen. */
  from: string | undefined;
  defaultBranch: string | undefined;
}

/** "New Branch": a Branch named by the person, started at the head of the Branch on screen. */
export function NewBranch({ domain, from, defaultBranch }: NewBranchProps) {
  const api = useKeeperApi();
  const toast = useToast();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');

  const create = useMutation({
    mutationFn: () =>
      api.branches.create(domain, {
        name: name.trim(),
        ...(from === undefined ? {} : { source: from }),
      }),
    onSuccess: async (branch) => {
      toast.notify(shellMessages.newBranch.created(branch.name, from), 'success');
      setOpen(false);
      setName('');
      await queryClient.invalidateQueries({ queryKey: ['keeper'] });
      await navigate({
        to: '.',
        search: (prev: ScopeSearch) => ({
          ...prev,
          branch: branch.name === defaultBranch ? undefined : branch.name,
        }),
      });
    },
  });

  return (
    <>
      <Button
        className="whitespace-nowrap"
        onClick={() => {
          setOpen(true);
        }}
      >
        {shellMessages.newBranch.open}
      </Button>
      <Dialog
        open={open}
        onClose={() => {
          setOpen(false);
        }}
        title={shellMessages.newBranch.title}
      >
        <form
          className="flex flex-col gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (name.trim() !== '') create.mutate();
          }}
        >
          <p className="text-sm text-muted">{shellMessages.newBranch.startsAt(from)}</p>
          <Input
            label={shellMessages.newBranch.name}
            hint={shellMessages.newBranch.nameHint}
            value={name}
            onChange={(event) => {
              setName(event.target.value);
            }}
          />
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              onClick={() => {
                setOpen(false);
              }}
            >
              {commonMessages.cancel}
            </Button>
            <Button
              type="submit"
              variant="primary"
              disabled={name.trim() === '' || create.isPending}
            >
              {shellMessages.newBranch.create}
            </Button>
          </div>
        </form>
      </Dialog>
    </>
  );
}
