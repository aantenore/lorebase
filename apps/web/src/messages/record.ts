/** Strings of the Record page. */
export const record = {
  openInCanvas: 'Open in Canvas',
  tabsLabel: 'Record sections',
  tabDetails: 'Details',
  tabHistory: 'History',
  attributes: {
    title: 'Attributes',
    caption: 'Attributes',
    columnAttribute: 'Attribute',
    columnValue: 'Value',
    columnType: 'Type',
    derived: 'derived',
  },
  links: {
    title: 'Linked Records',
    noRelations: 'This Kind has no Relations.',
    groupLabel: (name: string) => `${name} links`,
    none: 'No linked Records.',
  },
  lineage: {
    title: 'Lineage',
    caption: 'Lineage of each value',
    columnAttribute: 'Attribute',
    columnOrigin: 'Origin',
    columnValue: 'Value',
    columnDetail: 'Detail',
    columnRevision: 'Revision',
    columnAuthor: 'Author',
    columnAt: 'At',
    source: (name: string) => `Source ${name}`,
    operation: (name: string) => `Operation ${name}`,
    extraction: (e: {
      jobId: string;
      documentId: string;
      start: number;
      end: number;
      model: string;
      prompt: string;
    }) =>
      `Extraction job ${e.jobId}, document ${e.documentId} [${String(e.start)}-${String(e.end)}], model ${e.model}, prompt ${e.prompt}`,
    proposal: (title: string) => `Proposal ${title}`,
  },
  history: {
    title: 'History',
    empty: 'No changes on this Branch.',
    listLabel: 'Changes, newest first',
    changedAttributes: (revision: string) => `Attributes changed in ${revision}`,
    changedTo: 'changed to',
    loadMore: 'Load more',
  },
} as const;
