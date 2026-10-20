import type { Conflict, Operation, ProposalDetail, ProposalItem } from '../../types';
import {
  attr,
  builtinOperations,
  eid,
  kind,
  link,
  record,
  relation,
  revision,
  trait,
} from '../builders';
import type { BranchData, DomainData, ProposalState, SchemaBody } from '../state';

const DOMAIN = 'supply';

// ------------------------------------------------------------------ schema

const located = trait('Located', 'Anything with a country and a city.', [
  { id: eid('tat', 'located_country'), name: 'country', type: 'string', required: true },
  { id: eid('tat', 'located_city'), name: 'city', type: 'string', required: false },
]);

const locatedMap = (scope: string) => ({
  trait_id: located.id,
  attribute_map: {
    [eid('tat', 'located_country')]: eid('att', `${scope}_country`),
    [eid('tat', 'located_city')]: eid('att', `${scope}_city`),
  },
});

const supplier = kind(
  'Supplier',
  [
    attr('supplier', 'supplier_code', 'string', {
      required: true,
      unique: true,
      indexed: true,
      description: 'ERP vendor code.',
    }),
    attr('supplier', 'legal_name', 'string', {
      required: true,
      searchable: 'A',
      description: 'Registered legal name.',
    }),
    attr('supplier', 'country', 'string', {
      indexed: true,
      description: 'ISO 3166-1 alpha-2 code.',
      constraints: { pattern: '^[A-Z]{2}$' },
    }),
    attr('supplier', 'city', 'string', { searchable: 'C' }),
    attr('supplier', 'tier', 'enum', {
      description: 'Supplier tier owned by the ERP.',
      constraints: { enum_values: ['1', '2', '3'] },
      authority: eid('src', 'erp_vendors'),
    }),
    attr('supplier', 'risk_score', 'integer', {
      indexed: true,
      description: 'Composite risk score, 0 (none) to 100 (severe).',
      constraints: { min: 0, max: 100 },
    }),
  ],
  {
    description: 'A company that supplies Parts to one or more Plants.',
    key: 'supplier_code',
    title: 'legal_name',
    color: '#3a4fd6',
    traits: [locatedMap('supplier')],
  },
);

const part = kind(
  'Part',
  [
    attr('part', 'part_number', 'string', { required: true, unique: true, indexed: true }),
    attr('part', 'name', 'string', { required: true, searchable: 'A' }),
    attr('part', 'category', 'enum', {
      indexed: true,
      searchable: 'B',
      constraints: { enum_values: ['bearing', 'fastener', 'seal', 'casting', 'valve'] },
    }),
    attr('part', 'lead_time_days', 'integer', { constraints: { min: 0 } }),
    {
      ...attr('part', 'single_sourced', 'boolean', {
        description: 'True when the Part has exactly one supplied_by Link.',
      }),
      derivation_id: eid('drv', 'part_single_sourced'),
    },
  ],
  {
    description: 'A purchased component identified by part number.',
    key: 'part_number',
    title: 'name',
    color: '#1a7f4b',
  },
);

const plant = kind(
  'Plant',
  [
    attr('plant', 'plant_code', 'string', { required: true, unique: true }),
    attr('plant', 'name', 'string', { required: true, searchable: 'A' }),
    attr('plant', 'country', 'string', { indexed: true }),
    attr('plant', 'city', 'string', { searchable: 'C' }),
  ],
  {
    description: 'A manufacturing site that consumes Parts.',
    key: 'plant_code',
    title: 'name',
    color: '#b25e09',
    traits: [locatedMap('plant')],
  },
);

const contract = kind(
  'Contract',
  [
    attr('contract', 'contract_id', 'string', { required: true, unique: true }),
    attr('contract', 'title', 'string', { required: true, searchable: 'A' }),
    attr('contract', 'expiry_date', 'date', { indexed: true }),
  ],
  {
    description: 'A framework agreement with a Supplier.',
    key: 'contract_id',
    title: 'title',
    color: '#8a3ffc',
  },
);

const riskFlag = kind(
  'RiskFlag',
  [
    attr('riskflag', 'flag_id', 'string', { required: true, unique: true }),
    attr('riskflag', 'severity', 'enum', {
      required: true,
      constraints: { enum_values: ['low', 'medium', 'high', 'critical'] },
    }),
    attr('riskflag', 'reason', 'text', { required: true, searchable: 'B' }),
  ],
  {
    description: 'A recorded risk observation about a Supplier.',
    key: 'flag_id',
    title: 'flag_id',
    color: '#b42318',
  },
);

const suppliedBy = relation('supplied_by', {
  inverse: 'supplies',
  from: part,
  to: supplier,
  cardinality: 'many_to_many',
  description: 'The Suppliers that can deliver a Part.',
  attributes: [attr('suppliedby', 'is_primary', 'boolean')],
});
const feeds = relation('feeds', {
  inverse: 'fed_by',
  from: supplier,
  to: plant,
  cardinality: 'many_to_many',
  description: 'The Plants a Supplier delivers to.',
});
const withSupplier = relation('with_supplier', {
  inverse: 'has_contracts',
  from: contract,
  to: supplier,
  cardinality: 'many_to_one',
  requiredFrom: true,
  description: 'The Supplier a Contract is signed with.',
});
const flags = relation('flags', {
  inverse: 'flagged_by',
  from: riskFlag,
  to: supplier,
  cardinality: 'many_to_one',
  requiredFrom: true,
  description: 'The Supplier a RiskFlag is about.',
});

const flagSupplierRisk: Operation = {
  id: eid('opr', 'flag_supplier_risk'),
  api_name: 'flag_supplier_risk',
  display_name: 'Flag supplier risk',
  description: 'Flag a supplier as a risk with a reason and severity.',
  parameters: [
    { name: 'supplier', type: 'ref', required: true, description: '', kind_ref: 'Supplier' },
    {
      name: 'severity',
      type: 'enum',
      required: true,
      description: '',
      enum_values: ['low', 'medium', 'high', 'critical'],
    },
    { name: 'reason', type: 'text', required: true, description: 'Evidence-based reason' },
  ],
  preconditions: [
    {
      expression: 'record(supplier).status != "terminated"',
      message: 'Terminated suppliers cannot be flagged.',
    },
  ],
  steps: [
    {
      action: 'create_record',
      with: { kind: 'RiskFlag', values: { severity: 'params.severity', reason: 'params.reason' } },
    },
    { action: 'create_link', with: { relation: 'flags', from: '$step[0]', to: 'params.supplier' } },
  ],
  review_policy: 'params.severity == "critical"',
  permissions: ['risk-analyst', 'agent-risk'],
  side_effects: ['risk_flag_raised'],
  rate_limit: null,
  status: 'active',
  builtin: false,
};

const mainSchema: SchemaBody = {
  kinds: [supplier, part, plant, contract],
  relations: [suppliedBy, feeds, withSupplier],
  traits: [located],
  operations: builtinOperations([supplier, part, plant, contract]),
  derivations: [
    {
      id: eid('drv', 'part_single_sourced'),
      api_name: 'single_sourced',
      display_name: 'Single sourced',
      description: 'True when the Part has exactly one supplied_by Link.',
      variant: 'aggregate',
      target: { type: 'attribute', kind_id: part.id, api_name: 'single_sourced' },
      definition: { relation: 'supplied_by', aggregate: 'count', equals: 1 },
    },
  ],
  sources: [
    {
      id: eid('src', 'erp_vendors'),
      api_name: 'erp.vendors',
      type: 'postgres_table',
      connection: 'secret://env/ERP_DSN',
      target_kind: supplier.id,
      key_mapping: ['vendor_id'],
      attribute_mapping: {
        [eid('att', 'supplier_legal_name')]: { column: 'vendor_name' },
        [eid('att', 'supplier_country')]: { column: 'country_iso' },
        [eid('att', 'supplier_tier')]: { column: 'tier' },
      },
      relation_mapping: {},
      scope_filter: null,
      sync_mode: 'snapshot',
      schedule: '0 2 * * *',
      target_branch_policy: {
        branch_template: 'source/{source}',
        merge_policy: 'auto_with_authority',
      },
    },
  ],
  search: { modes: ['lexical'], embedding: null },
};

// -------------------------------------------------------------------- data

function mainData(): BranchData {
  const suppliers: [string, string, string, string, string, number][] = [
    ['S-1001', 'Brevik Bearings S.p.A.', 'IT', 'Torino', '2', 35],
    ['S-1002', 'Nordhaus Fasteners GmbH', 'DE', 'Stuttgart', '1', 12],
    ['S-1003', 'Kestrel Castings Ltd', 'GB', 'Sheffield', '3', 71],
    ['S-1004', 'Orlova Seals Sp. z o.o.', 'PL', 'Gliwice', '2', 48],
    ['S-1005', 'Tamura Precision KK', 'JP', 'Nagoya', '1', 20],
    ['S-1006', 'Valmora Hydraulics S.A.', 'ES', 'Bilbao', '3', 64],
  ];
  const parts: [string, string, string, number][] = [
    ['P-77812', 'Deep groove bearing 6204', 'bearing', 21],
    ['P-77813', 'Tapered roller bearing', 'bearing', 35],
    ['P-81020', 'Hex bolt M8', 'fastener', 7],
    ['P-81021', 'Flange nut M8', 'fastener', 7],
    ['P-90310', 'Radial shaft seal', 'seal', 14],
    ['P-90311', 'O-ring kit', 'seal', 10],
    ['P-55120', 'Gearbox housing', 'casting', 56],
    ['P-66001', 'Proportional valve', 'valve', 42],
  ];
  const supplies: [string, string, boolean][] = [
    ['P-77812', 'S-1001', true],
    ['P-77812', 'S-1005', false],
    ['P-77813', 'S-1001', true],
    ['P-81020', 'S-1002', true],
    ['P-81021', 'S-1002', true],
    ['P-90310', 'S-1004', true],
    ['P-90311', 'S-1004', true],
    ['P-55120', 'S-1003', true],
    ['P-66001', 'S-1006', true],
    ['P-66001', 'S-1005', false],
  ];
  const sourcesPerPart = (number: string) => supplies.filter(([p]) => p === number).length;

  return {
    protected: true,
    schema: structuredClone(mainSchema),
    records: [
      ...suppliers.map(([code, name, country, city, tier, risk]) =>
        record('Supplier', code, {
          supplier_code: code,
          legal_name: name,
          country,
          city,
          tier,
          risk_score: risk,
        }),
      ),
      ...parts.map(([number, name, category, lead]) =>
        record(
          'Part',
          number,
          { part_number: number, name, category, lead_time_days: lead },
          { single_sourced: sourcesPerPart(number) === 1 },
        ),
      ),
      record('Plant', 'TRN', {
        plant_code: 'TRN',
        name: 'Torino Assembly',
        country: 'IT',
        city: 'Torino',
      }),
      record('Plant', 'GLW', {
        plant_code: 'GLW',
        name: 'Gliwice Works',
        country: 'PL',
        city: 'Gliwice',
      }),
      record('Plant', 'MNZ', {
        plant_code: 'MNZ',
        name: 'Monza Foundry',
        country: 'IT',
        city: 'Monza',
      }),
      record('Contract', 'C-2231', {
        contract_id: 'C-2231',
        title: 'Brevik master supply agreement',
        expiry_date: '2027-03-31',
      }),
      record('Contract', 'C-2232', {
        contract_id: 'C-2232',
        title: 'Nordhaus fastener framework',
        expiry_date: '2026-12-31',
      }),
      record('Contract', 'C-2233', {
        contract_id: 'C-2233',
        title: 'Kestrel casting supply',
        expiry_date: '2026-11-30',
      }),
      record('Contract', 'C-2234', {
        contract_id: 'C-2234',
        title: 'Tamura precision parts',
        expiry_date: '2028-06-30',
      }),
    ],
    links: [
      ...supplies.map(([p, s, primary]) =>
        link('supplied_by', ['Part', p], ['Supplier', s], { is_primary: primary }),
      ),
      link('feeds', ['Supplier', 'S-1001'], ['Plant', 'TRN']),
      link('feeds', ['Supplier', 'S-1002'], ['Plant', 'TRN']),
      link('feeds', ['Supplier', 'S-1002'], ['Plant', 'GLW']),
      link('feeds', ['Supplier', 'S-1003'], ['Plant', 'MNZ']),
      link('feeds', ['Supplier', 'S-1004'], ['Plant', 'GLW']),
      link('feeds', ['Supplier', 'S-1005'], ['Plant', 'TRN']),
      link('feeds', ['Supplier', 'S-1006'], ['Plant', 'MNZ']),
      link('with_supplier', ['Contract', 'C-2231'], ['Supplier', 'S-1001']),
      link('with_supplier', ['Contract', 'C-2232'], ['Supplier', 'S-1002']),
      link('with_supplier', ['Contract', 'C-2233'], ['Supplier', 'S-1003']),
      link('with_supplier', ['Contract', 'C-2234'], ['Supplier', 'S-1005']),
    ],
    log: [
      revision(
        DOMAIN,
        'a4c91e07d2b8',
        'b83f60c1a5e9',
        'Sync erp.vendors run 7',
        '2026-10-19T02:04:11Z',
        'source_sync',
        {
          author: 'svc_scribe',
          changes: 6,
          originRef: 'run_7',
        },
      ),
      revision(
        DOMAIN,
        'b83f60c1a5e9',
        'c15d7a92e3f4',
        'Add Contract Kind and with_supplier Relation',
        '2026-10-14T15:30:00Z',
        'schema_change',
      ),
      revision(
        DOMAIN,
        'c15d7a92e3f4',
        'd9e0b4417c6a',
        'Sync erp.vendors run 6',
        '2026-10-12T02:03:40Z',
        'source_sync',
        {
          author: 'svc_scribe',
          changes: 4,
          originRef: 'run_6',
        },
      ),
      revision(
        DOMAIN,
        'd9e0b4417c6a',
        null,
        'Initial import of the supply Domain',
        '2026-10-05T09:00:00Z',
        'import',
        {
          changes: 31,
        },
      ),
    ],
  };
}

function derivedBranch(
  base: BranchData,
  newRevisions: BranchData['log'],
  mutate: (data: BranchData) => void,
): BranchData {
  const copy = structuredClone(base);
  copy.protected = false;
  copy.log = [...newRevisions, ...copy.log];
  mutate(copy);
  return copy;
}

function riskBranch(main: BranchData): BranchData {
  const ancestor = main.log[1];
  return derivedBranch(
    { ...main, log: main.log.slice(1) },
    [
      revision(
        DOMAIN,
        'f2a8c3d51b07',
        'e61d9f20a3c4',
        'Add flag_supplier_risk Operation',
        '2026-10-17T10:12:00Z',
        'schema_change',
      ),
      revision(
        DOMAIN,
        'e61d9f20a3c4',
        ancestor?.id ?? null,
        'Add RiskFlag Kind and flags Relation',
        '2026-10-16T16:45:00Z',
        'schema_change',
      ),
    ],
    (data) => {
      data.schema.kinds.push(riskFlag);
      data.schema.relations.push(flags);
      data.schema.operations.push(flagSupplierRisk);
      data.records.push(
        record('RiskFlag', 'F-1', {
          flag_id: 'F-1',
          severity: 'high',
          reason: 'Single foundry, repeated late deliveries in Q3.',
        }),
      );
      data.links.push(link('flags', ['RiskFlag', 'F-1'], ['Supplier', 'S-1003']));
    },
  );
}

function extractionBranch(main: BranchData): BranchData {
  return derivedBranch(
    main,
    [
      revision(
        DOMAIN,
        '3b7c0e94d1aa',
        main.log[0]?.id ?? null,
        'Extract contracts from documents (job 42)',
        '2026-10-19T11:20:00Z',
        'extraction',
        {
          author: 'svc_scribe',
          changes: 4,
          originRef: 'job_42',
        },
      ),
    ],
    (data) => {
      data.records.push(
        record('Contract', 'C-2235', {
          contract_id: 'C-2235',
          title: 'Orlova sealing framework',
          expiry_date: '2027-09-30',
        }),
        record('Contract', 'C-2236', {
          contract_id: 'C-2236',
          title: 'Valmora hydraulics supply',
          expiry_date: '2028-01-31',
        }),
      );
      data.links.push(
        link('with_supplier', ['Contract', 'C-2235'], ['Supplier', 'S-1004']),
        link('with_supplier', ['Contract', 'C-2236'], ['Supplier', 'S-1006']),
      );
    },
  );
}

function cleanupBranch(main: BranchData): BranchData {
  const ancestor = main.log[1];
  return derivedBranch(
    { ...main, log: main.log.slice(1) },
    [
      revision(
        DOMAIN,
        '7d12e5b8c0f3',
        ancestor?.id ?? null,
        'Set tier of S-1001 to 1',
        '2026-10-18T09:40:00Z',
        'operation',
        {
          author: 'usr_sofia',
        },
      ),
    ],
    (data) => {
      const target = data.records.find((r) => r.kind === 'Supplier' && r.key === 'S-1001');
      if (target !== undefined) target.values['tier'] = '1';
    },
  );
}

// -------------------------------------------------------------- proposals

const conflict = (id: string, code: Conflict['code'], rest: Partial<Conflict> = {}): Conflict => ({
  id,
  code,
  fingerprint: `fp_${id}`,
  partition: 'Supplier',
  element_key: 'S-1001',
  attribute_id: null,
  payload: { base: null, ours: null, theirs: null },
  member_count: 1,
  resolution: null,
  resolved_by: null,
  resolved_at: null,
  ...rest,
});

function proposal(
  detail: Omit<ProposalDetail, 'merged_revision' | 'reviews' | 'policies'> &
    Partial<ProposalDetail>,
): ProposalDetail {
  return { merged_revision: null, reviews: [], policies: [], ...detail };
}

function proposals(): Map<string, ProposalState> {
  const items: ProposalItem[] = [
    [
      'i1',
      'C-2235',
      'att_contract_expiry_date',
      'This Agreement expires on 30 September 2027 unless renewed.',
      [1280, 1342],
      0.96,
      'new',
    ],
    [
      'i2',
      'C-2235',
      'att_contract_title',
      'Orlova Seals sealing framework agreement',
      [12, 52],
      0.91,
      'matched',
    ],
    [
      'i3',
      'C-2236',
      'att_contract_expiry_date',
      'Term ends 31 January 2028.',
      [904, 930],
      0.78,
      'ambiguous',
    ],
    ['i4', 'C-2236', 'att_contract_title', 'Valmora hydraulics supply terms', [8, 40], 0.88, 'new'],
  ].map(([item_id, element_key, attribute_id, quote, span, confidence, match]) => ({
    item_id: item_id as string,
    partition: 'Contract',
    element_key: element_key as string,
    attribute_id: attribute_id as string,
    evidence: {
      document_id: 'doc_contracts_oct',
      page: 4,
      span: span as [number, number],
      quote: quote as string,
    },
    confidence: confidence as number,
    match_status: match as ProposalItem['match_status'],
    decision: 'pending',
    decided_by: null,
    decided_at: null,
  }));

  const states: ProposalState[] = [
    {
      detail: proposal({
        id: '41',
        title: 'Risk model: RiskFlag and flag_supplier_risk',
        description:
          'Adds the RiskFlag Kind, the flags Relation and the flag_supplier_risk Operation.',
        source_ref: 'model/risk',
        target_ref: 'main',
        origin: 'manual',
        status: 'open',
        author: 'usr_marco',
        created_at: '2026-10-17T10:30:00Z',
        checks: [
          { name: 'Schema validation', status: 'passed' },
          {
            name: 'Data consistency',
            status: 'failed',
            message: '3 Supplier Records have no risk_score.',
          },
          { name: 'Operation dry run', status: 'pending' },
        ],
        reviews: [
          {
            reviewer: 'usr_sofia',
            verdict: 'comment',
            comment: 'Please settle the risk_score range first.',
            at: '2026-10-18T08:15:00Z',
          },
        ],
      }),
      conflicts: [
        conflict('c1', 'SC1', {
          partition: 'schema',
          element_key: 'Supplier.risk_score',
          attribute_id: 'att_supplier_risk_score',
          payload: { base: { max: 100 }, ours: { max: 10 }, theirs: { max: 100 } },
        }),
        conflict('c2', 'XS4', {
          element_key: '3 Records',
          attribute_id: 'att_supplier_risk_score',
          member_count: 3,
        }),
      ],
      items: [],
    },
    {
      detail: proposal({
        id: '42',
        title: 'Contracts batch October',
        description: 'Proposes 2 new Contract Records, each with a quoted evidence span.',
        source_ref: 'extract/job-42',
        target_ref: 'main',
        origin: 'extraction',
        status: 'open',
        author: 'svc_scribe',
        created_at: '2026-10-19T11:25:00Z',
        checks: [
          { name: 'Schema validation', status: 'passed' },
          { name: 'Data consistency', status: 'passed' },
        ],
      }),
      conflicts: [],
      items,
    },
    {
      detail: proposal({
        id: '43',
        title: 'Tier cleanup',
        description: 'Normalizes Supplier tiers.',
        source_ref: 'cleanup/tiers',
        target_ref: 'main',
        origin: 'manual',
        status: 'open',
        author: 'usr_sofia',
        created_at: '2026-10-18T09:50:00Z',
        policies: [{ name: 'erp-authority', rule: 'authority' }],
        checks: [{ name: 'Schema validation', status: 'passed' }],
      }),
      conflicts: [
        conflict('c3', 'DV1', {
          attribute_id: 'att_supplier_tier',
          payload: { base: '2', ours: '1', theirs: '3' },
        }),
      ],
      items: [],
    },
    {
      detail: proposal({
        id: '40',
        title: 'Sync erp.vendors run 6',
        description: 'Nightly Source sync.',
        source_ref: 'source/erp.vendors',
        target_ref: 'main',
        origin: 'source_sync',
        status: 'merged',
        author: 'svc_scribe',
        created_at: '2026-10-12T02:04:00Z',
        merged_revision: 'c15d7a92e3f4',
        checks: [{ name: 'Schema validation', status: 'passed' }],
      }),
      conflicts: [],
      items: [],
    },
  ];
  return new Map(states.map((s) => [s.detail.id, s]));
}

// ------------------------------------------------------------------ domain

export function buildSupplyDomain(): DomainData {
  const main = mainData();
  return {
    domain: {
      id: DOMAIN,
      display_name: 'Supply',
      description: 'Suppliers, parts, plants and contracts of a fictional industrial distributor.',
      default_branch: 'main',
      settings: { search_language: 'simple' },
      created_at: '2026-10-05T09:00:00Z',
      created_by: 'usr_marco',
    },
    branches: new Map([
      ['main', main],
      ['model/risk', riskBranch(main)],
      ['extract/job-42', extractionBranch(main)],
      ['cleanup/tiers', cleanupBranch(main)],
    ]),
    proposals: proposals(),
  };
}
