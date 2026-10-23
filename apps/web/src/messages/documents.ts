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
    storedPlain: 'Stored without extraction.',
    storedQueued: (job: number) => `Stored; extraction job ${job.toString()} queued.`,
    results: 'Upload results',
    urlForm: 'Add a Document by URL',
    urlLabel: 'Document URL',
    urlHint: 'Only allowed hosts can be fetched.',
    filenameLabel: 'File name (optional)',
    fetch: 'Fetch Document',
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
    pending: '...',
    proposalLink: (id: string) => `Proposal #${id}`,
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
    passagesOnly: 'The parsed text is not available yet; the passages the items quote are shown.',
    truncated: (shown: string, total: string) =>
      `The text is long: the first ${shown} of ${total} characters are shown.`,
    textLabel: 'Parsed text',
  },
} as const;
