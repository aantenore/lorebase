import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from '@tanstack/react-router';
import { useState } from 'react';
import { useKeeperApi } from '@/api/provider';
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
      toast.notify(
        `Branch ${branch.name} created from ${from ?? 'the default Branch'}.`,
        'success',
      );
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
        size="sm"
        onClick={() => {
          setOpen(true);
        }}
      >
        New Branch
      </Button>
      <Dialog
        open={open}
        onClose={() => {
          setOpen(false);
        }}
        title="New Branch"
      >
        <form
          className="flex flex-col gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (name.trim() !== '') create.mutate();
          }}
        >
          <p className="text-sm text-muted">
            The Branch starts at the head of {from ?? 'the default Branch'}. Names may contain
            slashes, for example model/risk.
          </p>
          <Input
            label="Branch name"
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
              Cancel
            </Button>
            <Button
              type="submit"
              variant="primary"
              disabled={name.trim() === '' || create.isPending}
            >
              Create Branch
            </Button>
          </div>
        </form>
      </Dialog>
    </>
  );
}
