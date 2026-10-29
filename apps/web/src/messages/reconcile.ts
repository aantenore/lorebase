/** Strings of the Reconciliation screen and of the marks it leaves on other screens. */
export const reconcile = {
  title: 'Reconciliation',
  description: 'Values on which Sources disagree, and Records that may be duplicates.',
  docs: 'tour/#reconciliation',
  tabsLabel: 'Candidate types',
  types: {
    duplicate: 'Duplicates',
    contradiction: 'Contradictions',
    stale: 'Stale',
    superseded: 'Superseded',
  },
  /** One candidate's type, in a badge. */
  typeOne: {
    duplicate: 'duplicate',
    contradiction: 'contradiction',
    stale: 'stale',
    superseded: 'superseded',
  },
  tab: (label: string, count: string) => `${label} (${count})`,
  countMore: (count: number) => `${count.toString()}+`,
  countPending: '...',
  statuses: {
    open: 'Open',
    resolved: 'Resolved',
    dismissed: 'Dismissed',
    obsolete: 'Obsolete',
    all: 'All',
  },

  sweep: {
    run: 'Queue a sweep',
    queued: (job: number, branch: string) => `Sweep of ${branch} queued as job ${job.toString()}.`,
  },

  filters: {
    label: 'Filter candidates',
    status: 'Status',
    kind: 'Kind',
    allKinds: 'All Kinds',
    attribute: 'Attribute',
    allAttributes: 'All Attributes',
    attributeHint: 'Pick a Kind first.',
    source: 'Source identity',
    sourcePlaceholder: 'source:..., document:...',
    signal: 'Signal',
    allSignals: 'All signals',
    signalHint: 'Applies to the loaded pages.',
    clear: 'Clear filters',
  },

  list: {
    label: 'Candidates',
    empty: 'No candidates match these filters.',
    loadMore: 'Load more',
    select: (name: string) => `Select ${name}`,
    selectAll: 'Select every open candidate on screen',
    claims: (count: number) => `${count.toString()} ${count === 1 ? 'claim' : 'claims'}`,
    pick: 'Pick a candidate to see its claims.',
  },

  /** What the detection stages found. */
  signals: {
    label: 'Signals',
    unit: 'unit',
    negation: 'negation',
    similarity: (score: string) => `similarity ${score}`,
    equivalent: 'equivalent',
    judge: (label: string) => `judge: ${label}`,
    periods: 'periods',
    stale: (reasons: string) => `stale: ${reasons}`,
    normalized: 'same normalized value',
    trigram: 'similar titles',
    semantic: 'similar meaning',
    ambiguous: 'ambiguous mention',
    other: (name: string) => name,
    hints: {
      unit: 'The values carry different quantities once their units are read.',
      negation: 'The values differ by a negation word.',
      equivalent: 'The texts read as paraphrases of each other.',
      periods: 'The claims have validity periods that do not overlap.',
      source_unseen:
        'Its incremental Source has not read this Record upstream within stale_after: it may be gone there. Keep the current value, dismiss, or delete the Record.',
    } as Readonly<Record<string, string>>,
  },

  detail: {
    label: (title: string) => `Candidate ${title}`,
    raisedBy: 'Raised by Proposal',
    linkedProposal: 'Proposal',
    created: (at: string) => `Raised ${at}`,
    decided: (by: string, at: string) => `Decided by ${by}, ${at}`,
    reason: (reason: string) => `Reason: ${reason}`,
    claimsTitle: 'Claims',
    claimsMore: (shown: number, total: number) =>
      `${shown.toString()} of ${total.toString()} claims listed, newest first.`,
    recordsTitle: 'Records',
    noKind: 'The Kind of this candidate is no longer in the schema.',
  },

  claim: {
    label: (index: number) => `Claim ${index.toString()}`,
    current: 'current value',
    value: 'Value',
    source: 'Source',
    at: 'Time',
    period: 'Valid',
    periodValue: (from: string, to: string) => `${from} to ${to}`,
    open: 'open',
    none: '-',
    evidence: 'Evidence',
    viewInDocument: 'View in document',
    proposalItem: (proposal: string) => `Item of Proposal #${proposal}`,
    unknownSource: 'unknown source',
    /** Display of a source identity, `{type}:{id}`. */
    sources: {
      source: (name: string) => `Source ${name}`,
      document: (id: string) => `Document ${id}`,
      principal: (id: string) => `Person or service ${id}`,
      import: (batch: string) => `Import ${batch}`,
    } as Readonly<Record<string, (id: string) => string>>,
  },

  decide: {
    title: 'Decision',
    legend: 'Choice',
    choices: {
      claim: 'Take a claim',
      keep_current: 'Keep the current value',
      custom: 'Custom value',
      policy: 'Apply a policy',
    },
    claim: 'Claim to take',
    claimOption: (index: number, value: string, source: string) =>
      `${index.toString()}: ${value} (${source})`,
    custom: 'Value to write',
    policy: 'Policy',
    policies: {
      authority: 'Authority: the highest-ranked source',
      newest_wins: 'Newest wins: by period, else by time',
      prefer_current: 'Prefer the current value',
      prefer_incoming: 'Prefer the incoming value',
    },
    reason: 'Reason',
    reasonHint: 'Required. Kept with the decision in the audit log.',
    reasonRequired: 'Enter a reason.',
    pickClaim: 'Pick a claim.',
    enterValue: 'Enter a value.',
    preview: 'Preview',
    apply: 'Apply decision',
    dismiss: 'Dismiss',
    reopen: 'Reopen',
    reopenTitle: 'Reopen',
    reopenHint: 'The candidate was dismissed. Reopen it to decide again.',
    notOpen: (status: string) => `This candidate is ${status}; it takes no decision.`,
    previewTitle: 'Preview',
    previewNothing: 'Nothing would be written.',
    previewWrites: (count: number) =>
      `${count.toString()} ${count === 1 ? 'change' : 'changes'} would be written:`,
    previewClaim: (index: number) => `Picks claim ${index.toString()}.`,
  },

  merge: {
    title: 'Merge into one Record',
    survivor: 'Record that stays',
    survivorOption: (key: string) => `Keep ${key}`,
    absorbed: (key: string) => `${key} is deleted; its Links move to the Record that stays.`,
    valuesCaption: 'Values that differ',
    attribute: 'Attribute',
    keep: (key: string) => `Value of ${key}`,
    pick: (attribute: string, key: string) => `${attribute}: take the value of ${key}`,
    same: 'Every Attribute has the same value.',
    sensitiveKept: (names: readonly string[]) =>
      `Sensitive Attributes keep the value of the Record that stays: ${names.join(', ')}.`,
    absent: 'absent',
    apply: 'Merge through a Proposal',
    loading: 'Loading the Records...',
  },

  outcome: {
    resolved: 'Decision applied.',
    proposed: 'The Branch takes changes through review: a Proposal holds the decision.',
    openProposal: (id: string) => `Open Proposal #${id}`,
    undecided: 'The policy could not decide.',
    dismissed: 'Candidate dismissed.',
    reopened: 'Candidate reopened.',
    revision: (id: string) => `Revision ${id}`,
  },

  errors: {
    forbidden: 'Your credential cannot decide candidates on this Branch.',
    notOpen: 'Someone decided this candidate in the meantime. It is shown as it is now.',
    reconciliationOpen: 'Open reconciliation candidates block this merge.',
    pending: 'A decision on this candidate waits in an open Proposal. Merge or close it first.',
    stale: 'The value changed since this candidate was found. Run detection again before deciding.',
  },

  bulk: {
    legend: 'Bulk decision',
    scope: 'Apply to',
    scopeSelected: (count: number) => `${count.toString()} selected`,
    scopeFilter: 'Every open candidate of the filters',
    action: 'Action',
    actions: {
      policy: 'Apply a policy',
      dismiss: 'Dismiss',
    },
    reason: 'Reason',
    preview: 'Preview',
    apply: 'Apply',
    nothingSelected: 'Select candidates, or apply to the filters.',
    result: (count: number, counts: string) => `${count.toString()} decided: ${counts}.`,
    previewResult: (count: number) =>
      `${count.toString()} ${count === 1 ? 'candidate' : 'candidates'} would be decided.`,
    countPart: (status: string, count: number) => `${count.toString()} ${status}`,
  },

  /** Marks on the Record page. */
  record: {
    badge: (count: number) =>
      `${count.toString()} open ${count === 1 ? 'candidate' : 'candidates'}`,
    open: 'Review in Reconciliation',
    duplicate: (keys: string) => `Possible duplicate: ${keys}`,
  },

  /** The `reconciliation` check of a Proposal. */
  check: {
    candidates: 'Open candidates',
  },
} as const;
