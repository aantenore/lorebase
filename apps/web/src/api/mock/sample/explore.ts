import type { DomainExample } from '../../types';
import type { ExploreScript } from '../explore';

/**
 * Sample questions of the `supply` Domain (the mock serves them as `domain.examples`, like
 * examples/supply/schema.yaml does) and the steps the scripted guide plays for each.
 */
export const SUPPLY_EXAMPLES: readonly DomainExample[] = [
  {
    label: 'Single-source risk',
    question: 'Which parts depend on a single supplier, and how risky is that supplier?',
  },
  {
    label: 'Tier-1 high risk with open orders',
    question: 'Which tier-1 suppliers have a high risk score and open purchase orders?',
  },
  {
    label: 'What if S-1007 stops',
    question: 'What is the impact if supplier S-1007 stops delivering?',
  },
  { label: 'History of C-3013', question: 'What is the history of contract C-3013?' },
  { label: 'Typo search "bering"', question: 'Who supplies bering parts?' },
];

const BY_LABEL: Readonly<Record<string, ExploreScript>> = {
  'Single-source risk': {
    steps: [
      { thought: 'Part.single_sourced marks parts with one supplier; query those first.' },
      {
        tool: 'query',
        args: { kind: 'Part', where: { derived: 'single_sourced', op: 'eq', value: true } },
        summary: '7 records',
        refs: ['Part/P-55120', 'Part/P-90310', 'Part/P-90311', 'Part/P-77813', 'Part/P-10033'],
      },
      { thought: 'Follow supplied_by to the suppliers and compare their risk scores.' },
      {
        tool: 'traverse',
        args: { start: 'Part/P-55120', path: ['supplied_by'] },
        summary: '1 hops, 1 Records',
        refs: ['Part/P-55120', 'Supplier/S-1003'],
      },
      {
        tool: 'neighborhood',
        args: { refs: ['Part/P-90310', 'Part/P-77813'], depth: 1 },
        summary: '6 nodes, 5 edges',
        refs: ['Part/P-90310', 'Supplier/S-1004', 'Part/P-77813', 'Supplier/S-1001'],
      },
    ],
    answer:
      'Seven parts have a single supplier. The riskiest dependency is P-55120 (Gearbox housing) on Kestrel Castings Ltd (S-1003, risk score 71). Next come P-90310 and P-90311 on Orlova Seals (S-1004, risk 48) and P-77813 on Brevik Bearings (S-1001, risk 35).',
    cite: [
      'Part/P-55120',
      'Supplier/S-1003',
      'Part/P-90310',
      'Supplier/S-1004',
      'Part/P-77813',
      'Supplier/S-1001',
    ],
  },
  'Tier-1 high risk with open orders': {
    steps: [
      { thought: 'Look for tier-1 suppliers with a high risk score.' },
      {
        tool: 'query',
        args: { kind: 'Supplier', where: { attr: 'tier', op: 'eq', value: '1' } },
        summary: '2 records',
        refs: ['Supplier/S-1002', 'Supplier/S-1005'],
      },
      { thought: 'Both score 20 or less, and the Domain has no purchase orders to check.' },
      { tool: 'schema', args: {}, summary: '6 Kinds, 4 Relations' },
    ],
    answer: null,
  },
  'What if S-1007 stops': {
    steps: [
      { thought: 'Read Supplier S-1007 and the parts it supplies.' },
      {
        tool: 'get',
        args: { ref: 'Supplier/S-1007' },
        summary: 'Keeper error 404',
        ok: false,
      },
      {
        tool: 'search',
        args: { q: 'S-1007', mode: 'lexical' },
        summary: '0 hits (lexical)',
      },
    ],
    answer: null,
  },
  'History of C-3013': {
    steps: [
      { thought: 'Read the contract, then its change history.' },
      {
        tool: 'get',
        args: { ref: 'Contract/C-3013' },
        summary: 'Contract/C-3013',
        refs: ['Contract/C-3013'],
      },
      {
        tool: 'history',
        args: { ref: 'Contract/C-3013' },
        summary: '2 changes of Contract/C-3013',
        refs: ['Contract/C-3013'],
      },
      {
        tool: 'links',
        args: { ref: 'Contract/C-3013', relation: 'with_supplier' },
        summary: '1 links of Contract/C-3013',
        refs: ['Supplier/S-1013'],
      },
    ],
    answer:
      'C-3013 is the framework supply agreement with Wexrix Filtration (S-1013). It was created from the ERP import and changed once since, when Amendment 2 was extracted from its document.',
    cite: ['Contract/C-3013', 'Supplier/S-1013'],
  },
  'Typo search "bering"': {
    steps: [
      { thought: '"bering" is likely a typo of bearing; lexical search corrects close words.' },
      {
        tool: 'search',
        args: { q: 'bering', mode: 'lexical', limit: 10 },
        summary: '3 hits (lexical)',
        refs: ['Part/P-77812', 'Part/P-77813', 'Part/P-10033'],
      },
      {
        tool: 'neighborhood',
        args: { refs: ['Part/P-77812', 'Part/P-77813', 'Part/P-10033'], depth: 1 },
        summary: '6 nodes, 4 edges',
        refs: ['Supplier/S-1001', 'Supplier/S-1005', 'Supplier/S-1025'],
      },
    ],
    answer:
      'Bearings come from Brevik Bearings (S-1001: P-77812 and P-77813), Tamura Precision (S-1005, second source of P-77812) and Kavvik Filtration (S-1025: P-10033).',
    cite: [
      'Supplier/S-1001',
      'Part/P-77812',
      'Part/P-77813',
      'Supplier/S-1005',
      'Supplier/S-1025',
      'Part/P-10033',
    ],
  },
};

/** The scripts of the sample questions, by question text. */
export const SUPPLY_SCRIPTS: Readonly<Record<string, ExploreScript>> = Object.fromEntries(
  SUPPLY_EXAMPLES.flatMap((example) => {
    const script = BY_LABEL[example.label];
    return script === undefined ? [] : [[example.question, script]];
  }),
);
