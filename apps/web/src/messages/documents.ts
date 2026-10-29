/** Strings of the documents screens. */
export const documents = {
  title: 'Documents',
  description: 'Files that Lorebase reads into Proposals.',
  docs: 'extraction/#flow',
  branchBadge: (branch: string) => `Branch ${branch}`,
  loadMore: 'Load more',

  intake: {
    title: 'Add Documents',
    zoneLabel: 'Drop zone for Documents',
    prompt: 'Drop Documents here, or',
    inputLabel: 'Upload Documents',
    note: 'Each Document is stored once, whatever its name.',
    extraction: 'Extraction',
    extract: 'Extract after storing',
    tierNames: ['Rules', 'Local models', 'Chat model'],
    tier: (tier: number, name: string | undefined) =>
      name === undefined ? `Tier ${tier.toString()}` : `Tier ${tier.toString()} (${name})`,
    tiersMissing: 'Pick at least one tier, or turn extraction off.',
    uploading: 'Uploading...',
    upload: (n: number) => `Upload ${n.toString()} ${n === 1 ? 'Document' : 'Documents'}`,
    stored: (n: number) => `${n.toString()} ${n === 1 ? 'Document' : 'Documents'} stored.`,
    storedBefore: 'Stored before; nothing new was queued.',
    storedVersion: (version: number) => `Stored as version ${version.toString()}.`,
    storedPlain: 'Stored without extraction.',
    storedQueued: (job: number) => `Stored; extraction job ${job.toString()} queued.`,
    results: 'Upload results',
    urlForm: 'Add a Document by URL',
    urlLabel: 'Document URL',
    urlHint: 'Only allowed hosts can be fetched.',
    filenameLabel: 'File name (optional)',
    fetch: 'Fetch Document',
    sourceKeyLabel: 'Source key (optional)',
    sourceKeyHint: 'New bytes under the same key become a new version of the Document.',
  },

  library: {
    title: 'Library',
    empty: 'No Documents yet. Drop one above.',
    caption: 'Documents',
    document: 'Document',
    type: 'Type',
    size: 'Size',
    added: 'Added',
    by: 'By',
    extraction: 'Extraction',
    proposal: 'Proposal',
    version: 'Version',
    status: 'Status',
    pending: '...',
    proposalLink: (id: string) => `Proposal #${id}`,
  },

  lifecycle: {
    statuses: {
      active: 'active',
      superseded: 'superseded',
      retired: 'retired',
      purged: 'purged',
    },
    versionLabel: (version: number) => `v${version.toString()}`,
    sourceKey: 'Source key',
    version: 'Version',
    status: 'Status',
    details: 'Details',
    retiredBy: (by: string, at: string) => `Retired by ${by} on ${at}.`,
    purgedBy: (by: string, at: string) => `Purged by ${by} on ${at}.`,
    versionsTitle: 'Versions',
    versionsLabel: 'Versions of this Document',
    current: 'current',
    staleCandidates: (n: number) =>
      n === 1
        ? '1 open stale candidate comes from this Document.'
        : `${n.toString()} open stale candidates come from this Document.`,
    staleLink: 'Open them in Reconciliation',
    actions: 'Lifecycle',
    retire: 'Retire',
    retireTitle: 'Retire this Document',
    retireNote:
      'A retired Document stays on record but is no longer read; claims quoting it become stale.',
    retireConfirm: 'Retire Document',
    purge: 'Purge',
    purgeTitle: 'Purge this Document',
    purgeNote:
      'Purging deletes the stored file, its parsed text and what was derived from them. It cannot be undone.',
    purgeConfirm: 'Purge Document',
    reasonLabel: 'Reason',
    confirmLabel: (id: string) => `Type ${id} to confirm`,
    working: 'Working...',
    forbidden: 'Your credential is not allowed to do this.',
    purged: (n: { chunks: number; items: number }) =>
      `Purged: ${n.chunks.toString()} chunks and ${n.items.toString()} items removed.`,
  },

  page: {
    back: 'Back to Documents',
    meta: (type: string, size: string, added: string, by: string) =>
      `${type}, ${size}, added ${added} by ${by}.`,
    jobsTitle: 'Extraction jobs',
    textTitle: 'Parsed text',
  },

  jobs: {
    caption: 'Extraction jobs',
    empty: 'No extraction job was queued for this Document.',
    job: 'Job',
    status: 'Status',
    model: 'Model',
    prompt: 'Prompt',
    started: 'Started',
    finished: 'Finished',
    tokens: 'Tokens',
    cost: 'Cost',
    proposal: 'Proposal',
    failed: 'The extraction failed.',
    notExtracted: 'not extracted',
  },

  viewer: {
    items: 'Items quoting this Document',
    review: 'Review this item',
    passages: 'Evidence passages',
    characters: (start: number, end: number) =>
      `Characters ${start.toString()} to ${end.toString()}`,
    notParsed: 'The text of this Document has not been parsed yet.',
    wasPurged: 'This Document was purged: its text is no longer stored.',
    passagesOnly: 'The parsed text is not available yet; the passages the items quote are shown.',
    truncated: (shown: string, total: string) =>
      `The text is long: the first ${shown} of ${total} characters are shown.`,
    textLabel: 'Parsed text',
  },
} as const;
