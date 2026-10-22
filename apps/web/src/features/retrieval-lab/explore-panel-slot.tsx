import { useConfig } from '@/config/context';
import { PlaceholderSection } from '@/ui/layout';

/**
 * Where the Explore chat panel goes. It renders nothing without a guide service
 * (`guideUrl`); with one, a placeholder until the panel exists. The panel will call
 * `{guideUrl}/explore` and show the steps the guide takes as they stream in.
 */
export function ExplorePanelSlot() {
  const { guideUrl } = useConfig();
  if (guideUrl === null) return null;
  return (
    <PlaceholderSection
      area="Retrieval lab"
      screen="Explore (coming soon)"
      content="Ask the guide to explore the Domain and watch the steps it takes."
    />
  );
}
