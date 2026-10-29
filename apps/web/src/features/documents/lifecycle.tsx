import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { useState } from 'react';
import { describeError } from '@/api';
import { ForbiddenError } from '@/api/errors';
import type { DocumentDetail, DocumentStatus } from '@/api/extraction-types';
import { useKeeperApi } from '@/api/provider';
import { useDocumentVersions } from '@/api/queries';
import { useScopeSearch } from '@/app/scope';
import { common as commonMessages } from '@/messages/common';
import { documents as documentsMessages } from '@/messages/documents';
import { Button } from '@/ui/button';
import { Dialog } from '@/ui/dialog';
import { Input, Textarea } from '@/ui/field';
import { Badge, type BadgeTone } from '@/ui/layout';
import { shortTime } from '@/ui/time';

const T = documentsMessages.lifecycle;

export const STATUS_TONE: Record<DocumentStatus, BadgeTone> = {
  active: 'success',
  superseded: 'neutral',
  retired: 'warning',
  purged: 'danger',
};

/** Key, version, status and who retired or purged the document, with the stale candidates. */
export function LifecycleFacts({ detail, domain }: { detail: DocumentDetail; domain: string }) {
  const scopeSearch = useScopeSearch();
  return (
    <dl aria-label={T.details} className="mb-4 flex flex-col gap-1 text-sm">
      {detail.source_key !== null && (
        <div className="flex gap-2">
          <dt className="text-muted">{T.sourceKey}</dt>
          <dd className="font-mono">{detail.source_key}</dd>
        </div>
      )}
      <div className="flex gap-2">
        <dt className="text-muted">{T.version}</dt>
        <dd>{T.versionLabel(detail.version)}</dd>
      </div>
      <div className="flex items-center gap-2">
        <dt className="text-muted">{T.status}</dt>
        <dd>
          <Badge tone={STATUS_TONE[detail.status]}>{T.statuses[detail.status]}</Badge>
        </dd>
      </div>
      {detail.retired_at !== null && (
        <p>{T.retiredBy(detail.retired_by ?? '', shortTime(detail.retired_at))}</p>
      )}
      {detail.purged_at !== null && (
        <p>{T.purgedBy(detail.purged_by ?? '', shortTime(detail.purged_at))}</p>
      )}
      {detail.open_stale_candidates > 0 && (
        <p>
          {T.staleCandidates(detail.open_stale_candidates)}{' '}
          <Link
            to="/$domain/reconcile"
            params={{ domain }}
            search={{ ...scopeSearch, type: 'stale', source: `document:${detail.id}` }}
            className="text-accent hover:underline"
          >
            {T.staleLink}
          </Link>
        </p>
      )}
    </dl>
  );
}

/** The versions of the document's key, newest first, each linking to its page. */
export function Versions({ detail, domain }: { detail: DocumentDetail; domain: string }) {
  const scopeSearch = useScopeSearch();
  const versions = useDocumentVersions(domain, detail.id);
  if (versions.isPending) {
    return (
      <p role="status" className="text-muted">
        {commonMessages.loading}
      </p>
    );
  }
  if (versions.isError) {
    return (
      <p role="alert" className="text-danger">
        {versions.error.message}
      </p>
    );
  }
  return (
    <>
      <ul aria-label={T.versionsLabel} className="flex flex-col gap-1 text-sm">
        {versions.data.pages
          .flatMap((page) => page.items)
          .map((version) => (
            <li key={version.id} className="flex flex-wrap items-center gap-2">
              {version.id === detail.id ? (
                <span aria-current="true" className="font-medium">
                  {T.versionLabel(version.version)} ({T.current})
                </span>
              ) : (
                <Link
                  to="/$domain/documents/$document"
                  params={{ domain, document: version.id }}
                  search={scopeSearch}
                  className="text-accent hover:underline"
                >
                  {T.versionLabel(version.version)}
                </Link>
              )}
              <Badge tone={STATUS_TONE[version.status]}>{T.statuses[version.status]}</Badge>
              <span className="text-muted">
                {version.created_at === null ? '' : shortTime(version.created_at)}
              </span>
            </li>
          ))}
      </ul>
      {versions.hasNextPage && (
        <div className="mt-2">
          <Button
            disabled={versions.isFetchingNextPage}
            onClick={() => {
              void versions.fetchNextPage();
            }}
          >
            {documentsMessages.loadMore}
          </Button>
        </div>
      )}
    </>
  );
}

const errorText = (error: unknown) =>
  error instanceof ForbiddenError ? T.forbidden : describeError(error);

/** Retire and Purge, each behind a dialog; shown only to a caller who may do them. */
export function LifecycleActions({ detail, domain }: { detail: DocumentDetail; domain: string }) {
  const api = useKeeperApi();
  const queryClient = useQueryClient();
  const [dialog, setDialog] = useState<'retire' | 'purge' | null>(null);
  const [reason, setReason] = useState('');
  const [confirm, setConfirm] = useState('');
  const [outcome, setOutcome] = useState<string | null>(null);

  const close = () => {
    setDialog(null);
    setReason('');
    setConfirm('');
    retire.reset();
    purge.reset();
  };
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['keeper'] });

  const retire = useMutation({
    mutationFn: () => api.documents.retire(domain, detail.id, reason.trim()),
    meta: { inlineError: true },
    onSuccess: async () => {
      await refresh();
      close();
    },
  });
  const purge = useMutation({
    mutationFn: () =>
      api.documents.purge(domain, detail.id, { reason: reason.trim(), confirm: confirm.trim() }),
    meta: { inlineError: true },
    onSuccess: async (done) => {
      await refresh();
      setOutcome(T.purged(done));
      close();
    },
  });

  const canRetire = detail.can_retire && detail.status !== 'retired' && detail.status !== 'purged';
  const canPurge = detail.can_purge && detail.status !== 'purged';
  if (!canRetire && !canPurge) return outcome === null ? null : <p role="status">{outcome}</p>;

  const active = dialog === 'retire' ? retire : purge;
  const ready = reason.trim() !== '' && (dialog !== 'purge' || confirm.trim() === detail.id);
  return (
    <div className="mb-4 flex flex-wrap items-center gap-2" role="group" aria-label={T.actions}>
      {canRetire && (
        <Button
          onClick={() => {
            setDialog('retire');
          }}
        >
          {T.retire}
        </Button>
      )}
      {canPurge && (
        <Button
          variant="danger"
          onClick={() => {
            setDialog('purge');
          }}
        >
          {T.purge}
        </Button>
      )}
      {outcome !== null && <p role="status">{outcome}</p>}
      <Dialog
        open={dialog !== null}
        onClose={close}
        title={dialog === 'purge' ? T.purgeTitle : T.retireTitle}
      >
        <form
          className="flex flex-col gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (ready) active.mutate();
          }}
        >
          <p className="text-sm">{dialog === 'purge' ? T.purgeNote : T.retireNote}</p>
          <Textarea
            label={T.reasonLabel}
            value={reason}
            onChange={(event) => {
              setReason(event.target.value);
            }}
          />
          {dialog === 'purge' && (
            <Input
              label={T.confirmLabel(detail.id)}
              value={confirm}
              onChange={(event) => {
                setConfirm(event.target.value);
              }}
            />
          )}
          {active.isError && (
            <p role="alert" className="text-sm text-danger">
              {errorText(active.error)}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button onClick={close}>{commonMessages.cancel}</Button>
            <Button
              type="submit"
              variant={dialog === 'purge' ? 'danger' : 'primary'}
              disabled={!ready || active.isPending}
            >
              {active.isPending ? T.working : dialog === 'purge' ? T.purgeConfirm : T.retireConfirm}
            </Button>
          </div>
        </form>
      </Dialog>
    </div>
  );
}
