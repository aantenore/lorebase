import { attr, builtinOperations, kind, link, record, relation, revision } from '../builders';
import type { DomainData } from '../state';

const DOMAIN = 'hr';

const team = kind(
  'Team',
  [
    attr('team', 'team_code', 'string', { required: true, unique: true }),
    attr('team', 'name', 'string', { required: true, searchable: 'A' }),
  ],
  { description: 'An organizational unit.', key: 'team_code', title: 'name', color: '#0b7285' },
);

const employee = kind(
  'Employee',
  [
    attr('employee', 'employee_id', 'string', { required: true, unique: true }),
    attr('employee', 'full_name', 'string', {
      required: true,
      searchable: 'A',
      sensitivity: ['pii'],
    }),
    attr('employee', 'job_title', 'string', { searchable: 'B' }),
  ],
  {
    description: 'A person employed by the company.',
    key: 'employee_id',
    title: 'full_name',
    color: '#a61e4d',
  },
);

const memberOf = relation('member_of', {
  inverse: 'has_members',
  from: employee,
  to: team,
  cardinality: 'many_to_one',
  description: 'The Team an Employee belongs to.',
});

/** A second, small Domain so the Domain switcher has something to switch to. */
export function buildHrDomain(): DomainData {
  const log = [
    revision(
      DOMAIN,
      '51e0a7c93d28',
      null,
      'Initial import of the hr Domain',
      '2026-09-20T08:00:00Z',
      'import',
      {
        author: 'usr_luca',
        changes: 5,
      },
    ),
  ];
  return {
    domain: {
      id: DOMAIN,
      display_name: 'Human resources',
      description: 'People and teams. Separate grants; nothing links into it from other Domains.',
      default_branch: 'main',
      settings: {},
      created_at: '2026-09-20T08:00:00Z',
      created_by: 'usr_luca',
      head: null,
    },
    branches: new Map([
      [
        'main',
        {
          protected: true,
          schema: {
            kinds: [employee, team],
            relations: [memberOf],
            traits: [],
            operations: builtinOperations([employee, team]),
            derivations: [],
            sources: [],
            search: { modes: ['lexical'], embedding: null },
          },
          records: [
            record('Team', 'OPS', { team_code: 'OPS', name: 'Operations' }),
            record('Team', 'FIN', { team_code: 'FIN', name: 'Finance' }),
            record('Employee', 'E-01', {
              employee_id: 'E-01',
              full_name: 'Giulia Ferri',
              job_title: 'Planner',
            }),
            record('Employee', 'E-02', {
              employee_id: 'E-02',
              full_name: 'Tomas Berg',
              job_title: 'Controller',
            }),
          ],
          links: [
            link('member_of', ['Employee', 'E-01'], ['Team', 'OPS']),
            link('member_of', ['Employee', 'E-02'], ['Team', 'FIN']),
          ],
          log,
        },
      ],
    ]),
    proposals: new Map(),
  };
}
