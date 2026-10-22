/** A ready-made question offered as a button in the Explore panel. */
export interface Preset {
  id: string;
  label: string;
  question: string;
}

/**
 * Ready-made questions per Domain id. They fit the supply sample (examples/supply and the mock
 * API); other Domains get the free-text question only.
 */
export const PRESETS: Readonly<Record<string, readonly Preset[]>> = {
  supply: [
    {
      id: 'single-source',
      label: 'Single-source risk',
      question: 'Which parts depend on a single supplier, and how risky is that supplier?',
    },
    {
      id: 'tier1-risk',
      label: 'Tier-1 high risk with open orders',
      question: 'Which tier-1 suppliers have a high risk score and open purchase orders?',
    },
    {
      id: 'what-if',
      label: 'What if S-1007 stops',
      question: 'What is the impact if supplier S-1007 stops delivering?',
    },
    {
      id: 'history',
      label: 'History of C-3013',
      question: 'What is the history of contract C-3013?',
    },
    {
      id: 'typo',
      label: 'Typo search "bering"',
      question: 'Who supplies bering parts?',
    },
  ],
};

export function presetsFor(domain: string): readonly Preset[] {
  return Object.hasOwn(PRESETS, domain) ? (PRESETS[domain] ?? []) : [];
}
