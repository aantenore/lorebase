import { ExplorePanel } from '@/features/explore/explore-panel';

/**
 * The Explore panel under the Retrieval lab: questions to the guide service, its steps as they
 * stream in. It renders nothing in live mode without a guide service (`guideUrl`).
 */
export function ExplorePanelSlot() {
  return <ExplorePanel />;
}
