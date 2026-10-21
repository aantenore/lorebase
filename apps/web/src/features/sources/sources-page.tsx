import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { useKeeperApi } from '@/api/provider';
import { useSchema, useSourceRuns } from '@/api/queries';
import { useScope, useScopeSearch } from '@/app/scope';
import { PlannedScreens } from '@/features/planned-screens';
import { Badge, EmptyState, PageHeader, QueryBoundary, Section } from '@/ui/layout';
import { useToast } from '@/ui/toast';
import { RunsTable } from './runs-table';
import { SourcesEditor } from './sources-editor';
import { SourcesTable } from './sources-table';

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

  const sync = useMutation({
    mutationFn: (source: string) => api.sources.sync(scope.domain, source),
    onSuccess: async (queued) => {
      toast.notify(
        `Sync of ${queued.api_name} queued (job ${queued.job_id.toString()}).`,
        'success',
      );
      await queryClient.invalidateQueries({ queryKey: ['keeper', 'source-runs'] });
      await queryClient.invalidateQueries({ queryKey: ['keeper', 'audit'] });
    },
  });

  return (
    <>
      <PageHeader
        title="Sources"
        description={`Where the Records of ${scope.domainInfo.display_name} come from, and how they got here.`}
        actions={<Badge tone="accent">Branch {scope.branch}</Badge>}
      />
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
                  onSync={(source) => {
                    sync.mutate(source);
                  }}
                />
              )}
            </Section>

            <Section title="Run history">
              <QueryBoundary query={runs}>
                {(page) =>
                  page.items.length === 0 ? (
                    <EmptyState>No runs yet in {scope.domainInfo.display_name}.</EmptyState>
                  ) : (
                    <RunsTable domain={scope.domain} runs={page.items} />
                  )
                }
              </QueryBoundary>
            </Section>

            <Section title="Edit Sources (YAML)">
              <SourcesEditor key={data.revision} base={data} />
            </Section>
          </>
        )}
      </QueryBoundary>
      <PlannedScreens
        area="Sources"
        built={['Source list and detail', 'Documents and extraction jobs']}
      />
    </>
  );
}
