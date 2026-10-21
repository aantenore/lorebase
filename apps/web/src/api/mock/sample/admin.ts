import type { ApiKey, AuditEntry, Grant, Principal } from '../../admin-types';

/** Administration data of one Domain. */
export interface DomainAdminData {
  keys: ApiKey[];
  grants: Grant[];
  audit: AuditEntry[];
}

export interface AdminSeed {
  /** Installation-wide. */
  principals: Principal[];
  domains: Record<string, DomainAdminData>;
}

const principals: Principal[] = [
  {
    id: 'usr_marco',
    type: 'user',
    display_name: 'Marco Rossi',
    external_subject: 'marco@example.test',
    created_at: '2026-10-05T08:55:00Z',
  },
  {
    id: 'usr_luca',
    type: 'user',
    display_name: 'Luca Bianchi',
    external_subject: 'luca@example.test',
    created_at: '2026-09-20T07:50:00Z',
  },
  {
    id: 'svc_scribe',
    type: 'service',
    display_name: 'Scribe workers',
    external_subject: null,
    created_at: '2026-10-05T09:10:00Z',
  },
  {
    id: 'svc_herald',
    type: 'service',
    display_name: 'Herald (MCP agent)',
    external_subject: null,
    created_at: '2026-10-07T10:00:00Z',
  },
];

const entry = (
  seq: number,
  at: string,
  domain: string,
  actor: string,
  action: string,
  target: string | null,
  details: Record<string, unknown> = {},
): AuditEntry => ({
  seq,
  at,
  domain_id: domain,
  actor,
  action,
  target,
  details,
  entry_hash: '',
});

const grant = (
  id: string,
  domain: string,
  subject: string,
  role: string,
  createdBy: string,
  createdAt: string,
  scope: Pick<Grant, 'scope_type' | 'scope_ref'> = { scope_type: 'domain', scope_ref: null },
): Grant => ({
  id,
  domain_id: domain,
  subject,
  role,
  ...scope,
  created_by: createdBy,
  created_at: createdAt,
});

const supply: DomainAdminData = {
  keys: [
    {
      id: 'key_01',
      principal_id: 'svc_scribe',
      prefix: 'lbk_scr1',
      scopes: ['domain:supply'],
      expires_at: '2027-04-05T09:10:00Z',
      last_used_at: '2026-10-19T02:04:11Z',
      revoked_at: null,
      created_at: '2026-10-05T09:10:00Z',
    },
    {
      id: 'key_02',
      principal_id: 'svc_herald',
      prefix: 'lbk_hrl7',
      scopes: ['domain:supply'],
      expires_at: '2027-01-07T10:00:00Z',
      last_used_at: '2026-10-20T16:41:02Z',
      revoked_at: null,
      created_at: '2026-10-07T10:00:00Z',
    },
    {
      id: 'key_03',
      principal_id: 'svc_herald',
      prefix: 'lbk_hrl2',
      scopes: ['domain:supply'],
      expires_at: '2026-12-01T10:00:00Z',
      last_used_at: '2026-10-09T08:30:00Z',
      revoked_at: '2026-10-07T10:05:00Z',
      created_at: '2026-10-02T10:00:00Z',
    },
  ],
  grants: [
    grant('grt_01', 'supply', 'usr_marco', 'admin', 'usr_marco', '2026-10-05T09:00:00Z'),
    grant('grt_02', 'supply', 'usr_luca', 'modeler', 'usr_marco', '2026-10-05T09:05:00Z'),
    grant('grt_03', 'supply', 'usr_luca', 'reviewer', 'usr_marco', '2026-10-05T09:05:30Z'),
    grant('grt_04', 'supply', 'svc_scribe', 'editor', 'usr_marco', '2026-10-05T09:10:00Z', {
      scope_type: 'branch',
      scope_ref: 'source/*',
    }),
    grant('grt_05', 'supply', 'svc_herald', 'agent-reader', 'usr_marco', '2026-10-07T10:00:00Z'),
    grant('grt_06', 'supply', 'svc_herald', 'operator', 'usr_marco', '2026-10-07T10:01:00Z', {
      scope_type: 'operation',
      scope_ref: 'flag_supplier_risk',
    }),
  ],
  audit: [
    entry(1, '2026-10-05T09:00:00Z', 'supply', 'usr_marco', 'domain.create', 'supply'),
    entry(2, '2026-10-05T09:05:00Z', 'supply', 'usr_marco', 'grant.create', 'grt_02', {
      subject: 'usr_luca',
      role: 'modeler',
    }),
    entry(3, '2026-10-05T09:10:00Z', 'supply', 'usr_marco', 'apikey.create', 'key_01', {
      principal_id: 'svc_scribe',
      prefix: 'lbk_scr1',
      role: 'editor',
    }),
    entry(4, '2026-10-07T10:00:00Z', 'supply', 'usr_marco', 'apikey.create', 'key_02', {
      principal_id: 'svc_herald',
      prefix: 'lbk_hrl7',
      role: 'agent-reader',
    }),
    entry(5, '2026-10-07T10:05:00Z', 'supply', 'usr_marco', 'apikey.revoke', 'key_03', {
      principal_id: 'svc_herald',
    }),
    entry(6, '2026-10-12T02:03:40Z', 'supply', 'svc_scribe', 'branch.create', 'source/erp.vendors'),
    entry(7, '2026-10-14T15:30:00Z', 'supply', 'usr_luca', 'schema.change', 'main', {
      commands: 5,
    }),
    entry(8, '2026-10-17T11:12:00Z', 'supply', 'usr_luca', 'schema.infer', 'csv', {
      source: 'csv',
      files: 3,
      issues: 1,
    }),
    entry(9, '2026-10-19T02:04:11Z', 'supply', 'svc_scribe', 'source.sync', 'erp.vendors', {
      run: 'run_7',
      changes: 6,
    }),
  ],
};

const hr: DomainAdminData = {
  keys: [],
  grants: [grant('grt_h1', 'hr', 'usr_luca', 'admin', 'usr_luca', '2026-09-20T08:00:00Z')],
  audit: [entry(1, '2026-09-20T08:00:00Z', 'hr', 'usr_luca', 'domain.create', 'hr')],
};

export function buildAdminSeed(): AdminSeed {
  return structuredClone({ principals, domains: { supply, hr } });
}
