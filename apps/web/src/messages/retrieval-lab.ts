/** Strings of the Retrieval lab. */
export const retrievalLab = {
  title: 'Retrieval lab',
  description: 'Compare two search settings and see the context an agent would get.',
  docs: 'retrieval-lab/',
  askGuide: 'Ask the guide',
  question: {
    label: 'Question',
    placeholder: 'Ask about your data',
    examples: 'Example questions',
    ask: 'Ask',
  },
  lens: {
    label: 'Lens',
    none: 'No lens',
    hint: 'Searches (sets without Kinds) and the pack',
  },
  budget: {
    label: 'Pack budget (tokens)',
    hint: 'Empty: default',
  },
  vector: {
    title: 'Query vector',
    pasteLabel: 'Query vector (JSON array)',
    pastePlaceholder: '[0.12, -0.03, 0.4]',
    dimensions: (n: number) => `${n.toString()} dimensions`,
    /** No assistant: the question is searched by words until a vector is pasted. */
    manual: (model: string | null, dims: number | null) =>
      `Searching by words only. Paste a query vector${
        model === null || dims === null ? '' : ` of ${model} (${dims.toString()} dimensions)`
      } to use semantic and hybrid search.`,
    failed: (message: string) => `${message} Paste a vector to use semantic and hybrid search.`,
    embedding: 'Embedding the question...',
    embedsOnAsk: 'The question is embedded when you ask.',
    embedded: (model: string | undefined, dims: number) =>
      `Embedded${model === undefined ? '' : ` by ${model}`}: ${dims.toString()} dimensions.`,
    notJson: 'Not valid JSON. Paste an array of numbers, like [0.1, -0.2].',
    notVector: 'A vector is a non-empty JSON array of numbers.',
    noVector: 'The assistant sent no vector.',
    unreachable: 'The assistant could not be reached.',
    answered: (status: number) => `The assistant answered ${status.toString()} to /embed.`,
  },
  params: {
    legend: (name: string) => `Parameters ${name}`,
    mode: 'Mode',
    needsVector: (mode: string) => `${mode} (needs a vector)`,
    waiting: (mode: string, name: string) =>
      `${mode} needs a query vector. Until there is one, set ${name} searches by words only.`,
    kinds: (name: string) => `Kinds ${name}`,
    noKind: 'No Kind ticked: all Kinds.',
    fields: {
      lexicalWeight: { label: 'Word weight', hint: 'Weight of the word match' },
      semanticWeight: { label: 'Meaning weight', hint: 'Weight of the vector match' },
      rrfK: { label: 'Rank fusion constant', hint: 'Smooths the merged ranking' },
      trigramThreshold: { label: 'Typo similarity', hint: 'Minimum similarity for typo matches' },
      trigramWeight: { label: 'Typo weight', hint: 'Weight of typo matches' },
      typoCandidates: { label: 'Typo corrections', hint: 'Corrections tried per word' },
      candidates: { label: 'Candidates', hint: 'Candidates per ranking' },
      limit: { label: 'Results', hint: 'Results shown' },
    },
  },
  results: {
    title: 'Results',
    column: (name: string) => `Results ${name}`,
    askFirst: 'Ask a question to see the results.',
    searching: 'Searching...',
    corrected: 'Corrected:',
    noHits: 'No hits.',
    score: (score: number) => `score ${Number(score.toPrecision(4)).toString()}`,
    summary: (shared: number, onlyA: number, onlyB: number, reordered: number) =>
      `${shared.toString()} in both, ${onlyA.toString()} only in A, ${onlyB.toString()} only in B, ${reordered.toString()} at a different rank.`,
    onlyIn: (side: string) => `only in ${side}`,
    sameRank: (other: string) => `same rank in ${other}`,
    higher: (n: number, other: string) => `ranks ${n.toString()} higher than in ${other}`,
    lower: (n: number, other: string) => `ranks ${n.toString()} lower than in ${other}`,
  },
  pack: {
    title: 'Context pack',
    askFirst: 'Ask a question to see its pack.',
    building: 'Building the pack...',
    noSeeds: 'The search found no Record to seed the pack.',
    caption: 'Records in the context pack',
    columns: { record: 'Record', role: 'Role', why: 'Why it is included' },
    valuesTrimmed: '(values trimmed)',
    links: (n: number) => `Links in the pack (${n.toString()})`,
    tokensPerSection: 'Tokens per section',
    usage: (used: number, limit: number) => `${used.toString()} of ${limit.toString()} tokens`,
    section: (name: string, tokens: number, percent: number) =>
      `${name}: ${tokens.toString()} (${percent.toString()}%)`,
    overBudget: 'over budget',
    capped: 'capped',
    toFit: (parts: string) => `To fit the budget: ${parts}.`,
    seed: 'seed',
    neighbor: 'neighbor',
    matched: (question: string) => `Matched the search "${question}"`,
    depth: (n: number, reached: string) => `Depth ${n.toString()}, ${reached}`,
    notInPack: 'its Link is not in the pack',
    reachedVia: (relation: string, from: string, inverse: boolean) =>
      `reached via ${inverse ? 'inverse of ' : ''}${relation} from ${from}`,
    removed: (step: string, n: number) => `${step}: ${n.toString()} removed`,
    leftOut: (section: string, n: number) => `${section}: ${n.toString()} left out`,
  },
  involved: {
    title: 'Records involved',
    label: 'Involved Records',
    none: 'No Records involved yet.',
    caption:
      'Records involved (search hits of both sets and the pack), drawn with their neighbors.',
    citedLabel: 'Cited Records',
    citedCaption: 'Records cited by the answer, drawn with their neighbors.',
  },
} as const;
