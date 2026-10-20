import { PlaceholderSection, Section } from '@/ui/layout';
import { screensOf, type Area } from './inventory';

/** Placeholders for the inventory screens of `area` that are not built yet. */
export function PlannedScreens({ area, built = [] }: { area: Area; built?: string[] }) {
  const planned = screensOf(area).filter((s) => !built.includes(s.screen));
  if (planned.length === 0) return null;
  return (
    <Section title={`${area}: planned screens`}>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {planned.map((s) => (
          <PlaceholderSection key={s.screen} area={s.area} screen={s.screen} content={s.content} />
        ))}
      </div>
    </Section>
  );
}
