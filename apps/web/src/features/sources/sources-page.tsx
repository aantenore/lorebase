import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { useKeeperApi } from '@/api/provider';
import { useSchema, useSourceRuns } from '@/api/queries';
import { useScope, useScopeSearch } from '@/app/scope';
import { sources as sourcesMessages } from '@/messages/sources';
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
      toast.notify(sourcesMessages.syncQueued(queued.api_name, queued.job_id), 'success');
      await queryClient.invalidateQueries({ queryKey: ['keeper', 'source-runs'] });
      await queryClient.invalidateQueries({ queryKey: ['keeper', 'audit'] });
    },
  });

  return (
    <>
      <PageHeader
        title={sourcesMessages.title}
        description={sourcesMessages.description}
        docs={sourcesMessages.docs}
        actions={<Badge tone="accent">{sourcesMessages.branchBadge(scope.branch)}</Badge>}
      />
      <QueryBoundary query={schema}>
        {(data) => (
          <>
            <Section title={sourcesMessages.title}>
              {data.sources.length === 0 ? (
                <EmptyState>
                  {sourcesMessages.emptyBeforeLink}
                  <Link
                    to="/$domain/modeler"
                    params={{ domain: scope.domain }}
                    search={{ ...scopeSearch, view: 'start' }}
                    className="text-accent hover:underline"
                  >
                    {sourcesMessages.modelerLink}
                  </Link>
                  {sourcesMessages.emptyAfterLink}
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

            <Section title={sourcesMessages.runHistory}>
              <QueryBoundary query={runs}>
                {(page) =>
                  page.items.length === 0 ? (
                    <EmptyState>{sourcesMessages.noRuns}</EmptyState>
                  ) : (
                    <RunsTable domain={scope.domain} runs={page.items} />
                  )
                }
              </QueryBoundary>
            </Section>

            <Section title={sourcesMessages.editSection}>
              <SourcesEditor key={data.revision} base={data} />
            </Section>
          </>
        )}
      </QueryBoundary>
    </>
  );
}
