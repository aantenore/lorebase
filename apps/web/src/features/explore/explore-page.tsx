import { useConfig } from '@/config/context';
import { EmptyState, PageHeader } from '@/ui/layout';
import { ExplorePanel } from './explore-panel';

/** The Explore screen: the panel on its own, next to the Retrieval lab in the navigation. */
export function ExplorePage() {
  const { apiMode, guideUrl } = useConfig();
  if (apiMode === 'live' && guideUrl === null) {
    return (
      <>
        <PageHeader title="Explore" />
        <EmptyState>
          No guide service is configured (guideUrl), so there is nobody to ask. See docs/guide.md to
          turn the assistant on.
        </EmptyState>
      </>
    );
  }
  return <ExplorePanel headingLevel={1} />;
}
