/** Strings of the Explorer screens: home, search and the Kind table. */
export const explorer = {
  title: 'Explorer',
  description: 'Search and browse Records.',
  docs: 'tour/#explorer',
  search: {
    label: 'Search Records',
    placeholder: 'Name, code or text',
    kind: 'Kind',
    resultsCaption: 'Search results',
    columnRecord: 'Record',
    columnKind: 'Kind',
    columnMatch: 'Match',
    noMatches: (q: string) => `No Records match "${q}".`,
  },
  home: {
    kinds: 'Kinds',
    kindsLabel: 'Kinds',
    noKinds: 'No Kinds yet.',
    recentRevisions: 'Recent Revisions',
    noRevisions: 'No Revisions yet.',
    openProposals: 'Open Proposals',
    noProposals: 'No open Proposals.',
  },
  kind: {
    description: 'Records of this Kind.',
    missing: (kind: string) => `Kind ${kind} does not exist.`,
    noRecords: (kind: string) => `No ${kind} Records.`,
    sectionTitle: (kindName: string) => `${kindName} Records`,
    tableCaption: (kind: string) => `${kind} Records`,
    nextPage: 'Next page',
    sortedAsc: ' (asc)',
    sortedDesc: ' (desc)',
  },
  value: {
    yes: 'Yes',
    no: 'No',
  },
} as const;
