/** Strings of the extraction screens. */
export const extraction = {
  docs: 'extraction/#flow',
  accept: 'Accept',
  reject: 'Reject',
  backToPending: 'Back to pending',
  noItems: 'This Proposal has no extraction items.',
  noMatch: 'No item matches the filters.',
  noneInProposal: 'This Proposal has no items.',
  keys: 'Keys in the table: arrows or j and k move, a accepts, r rejects, u resets.',

  summary: {
    decidedLabel: 'Decided items',
    decided: (decided: number, total: number) =>
      `${decided.toString()} of ${total.toString()} decided`,
    accepted: (count: number) => `${count.toString()} accepted`,
    pending: (count: number) => `${count.toString()} pending`,
    rejected: (count: number) => `${count.toString()} rejected`,
    meanConfidence: (percent: string) => `mean confidence ${percent}`,
    nothingStaged: 'Nothing staged.',
    staged: (count: number) => `${count.toString()} staged.`,
    discard: 'Discard staged',
    apply: (count: number) => `Apply decisions (${count.toString()})`,
  },

  outcome: {
    label: 'Last applied decisions',
    applied: (items: number, changes: number, skipped: number, revision: string | null) =>
      `Applied ${items.toString()} items: ${changes.toString()} changes on the Branch, ${skipped.toString()} skipped${
        revision === null ? '' : `, Revision ${revision}`
      }.`,
    toast: (decisions: number, changes: number, branch: string) =>
      `${decisions.toString()} decisions applied: ${changes.toString()} changes on ${branch}.`,
  },

  refusal: {
    label: 'Decisions refused',
    nothingChanged: (message: string) =>
      `${message} Nothing changed. Fix the staged decisions and apply again.`,
    showItem: 'Show item',
  },

  types: {
    record: 'New Records',
    value: 'Attribute values',
    link: 'Links',
  },

  describe: {
    link: (relation: string | null, from: string | null, to: string | null) =>
      `${relation ?? 'link'}: ${from ?? '?'} to ${to ?? '?'}`,
    value: (
      kind: string | null,
      key: string | null,
      attribute: string | null,
      current: string,
      value: string,
    ) => `${kind ?? ''} ${key ?? ''}: ${attribute ?? ''} ${current} to ${value}`,
    record: (kind: string | null, key: string | null) => `${kind ?? ''} ${key ?? ''}`,
  },

  table: {
    caption: 'Extraction items',
    columns: {
      decision: 'Decision',
      change: 'Change',
      confidence: 'Confidence',
      match: 'Match',
      evidence: 'Evidence',
      actions: 'Actions',
    },
    group: (label: string, count: number) => `${label} (${count.toString()})`,
    staged: 'staged',
    accept: (label: string) => `Accept ${label}`,
    reject: (label: string) => `Reject ${label}`,
  },

  filters: {
    label: 'Filter items',
    status: 'Status',
    statusOptions: {
      all: 'All',
      pending: 'Pending',
      accepted: 'Accepted',
      rejected: 'Rejected',
    },
    target: 'Kind or Relation',
    all: 'All',
    type: 'Type',
    min: 'Confidence at least',
    max: 'Confidence below',
    minPlaceholder: '0.0',
    maxPlaceholder: '1.0',
    badNumber: 'A number from 0 to 1.',
    clear: 'Clear filters',
  },

  bulk: {
    label: 'Bulk decisions',
    accept: (count: number) => `Accept ${count.toString()} shown`,
    reject: (count: number) => `Reject ${count.toString()} shown`,
    reset: (count: number) => `Reset ${count.toString()} shown`,
    threshold: 'Threshold',
    acceptAbove: (count: number, percent: string) =>
      `Accept ${count.toString()} pending at or above ${percent}`,
    rejectBelow: (count: number, percent: string) =>
      `Reject ${count.toString()} pending below ${percent}`,
  },

  detail: {
    label: (description: string) => `Item ${description}`,
    noEvidence: 'This item quotes no evidence.',
    location: (document: string, start: number, end: number) =>
      `${document}, characters ${start.toString()} to ${end.toString()}`,
    openInDocument: 'Open in document',
    method: (method: string) => `by ${method}`,
    score: (percent: string) => `score ${percent}`,
    missingValues: (names: string) => `Missing values: ${names}.`,
    missingLinks: (names: string) => `Needs a ${names} Link: accept it together with this Record.`,
    tier: (tier: number, model: string | null) =>
      `tier ${tier.toString()}${model === null ? '' : `, ${model}`}`,
    decidedBy: (decision: string, by: string) => `${decision} by ${by}`,
    notApplied: (reason: string) => `Accepted, but not applied to the Branch: ${reason}`,
    current: 'Current:',
    proposed: 'Proposed:',
  },

  editor: {
    label: 'Edit the item',
    from: (relation: string | null) => `From (${relation ?? 'Record'} key)`,
    to: 'To (Record key)',
    valueOf: (attribute: string | null) => `Value of ${attribute ?? 'the Attribute'}`,
    linkNeedsKeys: 'Both ends of a Link need a key.',
    stage: 'Stage edits',
    problem: (name: string, problem: string) => `${name}: ${problem}`,
  },

  picker: {
    ambiguous: 'Which Record is meant?',
    others: 'Other Records it could mean',
    score: (percent: string, method: string) => `${percent} by ${method}`,
  },

  match: {
    notAvailable: 'n/a',
    incomplete: ' (incomplete)',
  },

  confidence: {
    label: 'Confidence',
    none: 'no score',
  },

  evidence: {
    view: 'View in document',
  },
} as const;
