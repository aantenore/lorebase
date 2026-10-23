/** Strings of the sources screens. */
export const sources = {
  title: 'Sources',
  description: 'Where Records come from, and how they got here.',
  docs: 'tour/#sources',
  branchBadge: (branch: string) => `Branch ${branch}`,
  syncQueued: (source: string, job: number) => `Sync of ${source} queued (job ${job.toString()}).`,
  emptyBeforeLink: 'No Sources yet. Start from data in the ',
  modelerLink: 'Modeler',
  emptyAfterLink: ' or add one in the YAML editor below.',
  runHistory: 'Run history',
  noRuns: 'No runs yet.',
  editSection: 'Edit as YAML',

  table: {
    caption: 'Sources',
    source: 'Source',
    type: 'Type',
    mapping: 'Mapping',
    policy: 'Policy',
    schedule: 'Schedule',
    lastRun: 'Last run',
    lastRunCounts: 'Last run counts',
    actions: 'Actions',
    manual: 'manual',
    unavailable: 'n/a',
    neverRun: 'never run',
    rows: (n: number) => `${n.toString()} rows`,
    sync: 'Sync now',
    syncing: 'Syncing...',
    syncLabel: (source: string) => `Sync ${source} now`,
  },

  runs: {
    caption: 'Run history',
    run: 'Run',
    source: 'Source',
    status: 'Status',
    trigger: 'Trigger',
    started: 'Started',
    rowsRead: 'Rows read',
    changes: 'Changes',
    branch: 'Branch',
    proposal: 'Proposal',
    noProposal: 'none',
  },

  summary: {
    attributesInto: (n: number, kind: string) =>
      `${n.toString()} ${n === 1 ? 'Attribute' : 'Attributes'} into ${kind}`,
    relations: (n: number) => `${n.toString()} ${n === 1 ? 'Relation' : 'Relations'}`,
    key: (columns: string) => `key ${columns}`,
  },

  editor: {
    hint: 'Connections are secret references, never literals.',
    reset: 'Reset',
    loading: 'Loading editor...',
    yamlLabel: 'Sources YAML',
    fixYaml: 'Fix the YAML errors first.',
    fixIssues: 'Fix the validation issues first.',
    defaultMessage: 'Change the Sources',
    onlySection: (section: string) =>
      `Only the ${section}: section is edited here, as a mapping of Source names.`,
  },
} as const;
