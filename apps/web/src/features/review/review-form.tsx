import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useKeeperApi } from '@/api/provider';
import type { ProposalDetail, ReviewVerdict } from '@/api/types';
import { review as reviewMessages } from '@/messages/review';
import { Button } from '@/ui/button';
import { Select, Textarea } from '@/ui/field';
import { Badge } from '@/ui/layout';
import { useToast } from '@/ui/toast';

const text = reviewMessages.form;

const VERDICTS: { value: ReviewVerdict; label: string }[] = [
  { value: 'approve', label: text.verdicts.approve },
  { value: 'request_changes', label: text.verdicts.request_changes },
  { value: 'comment', label: text.verdicts.comment },
];

/** What the target Branch still needs before it takes the merge, in one line. */
export function approvalSummary(detail: ProposalDetail): string {
  const { approval } = detail;
  if (!approval.protected) return text.noApproval;
  const parts = [text.approvals(approval.approvals, approval.required)];
  if (approval.changes_requested.length > 0) {
    parts.push(text.changesRequested(approval.changes_requested.join(', ')));
  }
  return parts.join('; ');
}

/** Reviews of an open Proposal: where the approvals stand, and a form to add one. */
export function ReviewForm({ domain, proposal }: { domain: string; proposal: ProposalDetail }) {
  const api = useKeeperApi();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [verdict, setVerdict] = useState<ReviewVerdict>('approve');
  const [comment, setComment] = useState('');

  const submit = useMutation({
    mutationFn: () =>
      api.proposals.review(domain, proposal.id, {
        verdict,
        ...(comment.trim() === '' ? {} : { comment: comment.trim() }),
      }),
    onSuccess: async () => {
      toast.notify(text.added, 'success');
      setComment('');
      await queryClient.invalidateQueries({ queryKey: ['keeper'] });
    },
  });

  return (
    <div className="flex flex-col gap-3">
      <p className="flex flex-wrap items-center gap-2 text-sm">
        <Badge tone={proposal.approval.satisfied ? 'success' : 'warning'}>
          {proposal.approval.satisfied ? text.approved : text.waiting}
        </Badge>
        <span>{approvalSummary(proposal)}</span>
      </p>
      {proposal.status === 'open' && (
        <form
          aria-label={text.label}
          className="flex max-w-xl flex-col gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            submit.mutate();
          }}
        >
          <div className="w-56 max-w-full">
            <Select
              label={text.verdict}
              value={verdict}
              options={VERDICTS}
              onChange={(event) => {
                setVerdict(event.target.value as ReviewVerdict);
              }}
            />
          </div>
          <Textarea
            label={text.comment}
            value={comment}
            rows={2}
            onChange={(event) => {
              setComment(event.target.value);
            }}
          />
          <div>
            <Button type="submit" disabled={submit.isPending}>
              {text.submit}
            </Button>
          </div>
        </form>
      )}
    </div>
  );
}
