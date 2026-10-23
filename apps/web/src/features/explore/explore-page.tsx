import { explore as exploreMessages } from '@/messages/explore';
import { useConfig } from '@/config/context';
import { EmptyState, PageHeader } from '@/ui/layout';
import { LearnMore } from '@/ui/learn-more';
import { ExplorePanel } from './explore-panel';

const T = exploreMessages;

/** The Explore screen: questions to the optional assistant, streamed step by step. */
export function ExplorePage() {
  const { apiMode, guideUrl } = useConfig();
  if (apiMode === 'live' && guideUrl === null) {
    return (
      <>
        <PageHeader title={T.title} />
        <EmptyState>
          {T.noGuide} <LearnMore path={T.noGuideDocs} />
        </EmptyState>
      </>
    );
  }
  return (
    <>
      <PageHeader title={T.title} description={T.description} docs={T.docs} />
      <ExplorePanel />
    </>
  );
}
