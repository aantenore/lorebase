import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { isApiError } from '@/api';
import { useKeeperApi } from '@/api/provider';
import { useSchema, useSourceRuns } from '@/api/queries';
import { useScope, useScopeSearch } from '@/app/scope';
import { PlannedScreens } from '@/features/planned-screens';
import { Badge, EmptyState, PageHeader, QueryBoundary, Section } from '@/ui/layout';
import { useToast } from '@/ui/toast';
import { RunsTable } from './runs-table';
import { SourcesEditor } from './sources-editor';
import { SourcesTable } from './sources-table';

/** Keeper answers 501 `not_supported` while it publishes no routes for Source runs. */
const NOT_SUPPORTED = 'not_supported';

/**
 * Sources (design 12.3): the Sources of the schema with mapping, policy, schedule and last
 * run, a run history linking the Proposals runs opened, "Sync now", and a YAML editor.
 */
export function SourcesPage() {
  const scope = useScope();
  const api = useKeeperApi();
  const toast = useToast();
  const queryClient = useQueryClient();
  const scopeSearch = useScopeSearch();
  const schema = useSchema(scope);
  const runs = useSourceRuns(scope.domain);
  const runsUnavailable =
    runs.isError && isApiError(runs.error) && runs.error.code === NOT_SUPPORTED;

  const sync = useMutation({
    mutationFn: (source: string) => api.sources.sync(scope.domain, source),
    onSuccess: async (run) => {
      toast.notify(`Run ${run.id} of ${run.source} ${run.status}.`, 'success');
      await queryClient.invalidateQueries({ queryKey: ['keeper', 'source-runs'] });
      await queryClient.invalidateQueries({ queryKey: ['keeper', 'audit'] });
    },
    onError: (error) => {
      toast.notify(isApiError(error) ? error.message : 'The sync failed.', 'error');
    },
  });

  return (
    <>
      <PageHeader
        title="Sources"
        description={`Where the Records of ${scope.domainInfo.display_name} come from, and how they got here.`}
        actions={<Badge tone="accent">Branch {scope.branch}</Badge>}
      />
      {runsUnavailable && (
        <p
          role="note"
          className="mb-4 rounded-md border border-line bg-surface p-3 text-sm text-muted"
        >
          Keeper does not publish Source runs yet, so last run status, the run history and Sync now
          are available in mock mode only. The Sources below come from the schema.
        </p>
      )}
      <QueryBoundary query={schema}>
        {(data) => (
          <>
            <Section title={`Sources on ${scope.branch}`}>
              {data.sources.length === 0 ? (
                <EmptyState>
                  This Branch has no Sources. Start from your data in the{' '}
                  <Link
                    to="/$domain/modeler"
                    params={{ domain: scope.domain }}
                    search={{ ...scopeSearch, view: 'start' }}
                    className="text-accent hover:underline"
                  >
                    Modeler
                  </Link>{' '}
                  or add one in the YAML editor below.
                </EmptyState>
              ) : (
                <SourcesTable
                  schema={data}
                  runs={runs.data?.items}
                  syncing={sync.isPending ? (sync.variables ?? null) : null}
                  canSync={!runsUnavailable}
                  onSync={(source) => {
                    sync.mutate(source);
                  }}
                />
              )}
            </Section>

            <Section title="Run history">
              {runsUnavailable ? (
                <EmptyState>No run history: Keeper does not publish Source runs yet.</EmptyState>
              ) : (
                <QueryBoundary query={runs}>
                  {(page) =>
                    page.items.length === 0 ? (
                      <EmptyState>No runs yet in {scope.domainInfo.display_name}.</EmptyState>
                    ) : (
                      <RunsTable domain={scope.domain} runs={page.items} />
                    )
                  }
                </QueryBoundary>
              )}
            </Section>

            <Section title="Edit Sources (YAML)">
              <SourcesEditor key={data.revision} base={data} />
            </Section>
          </>
        )}
      </QueryBoundary>
      <PlannedScreens area="Sources" built={['Source list and detail']} />
    </>
  );
}
