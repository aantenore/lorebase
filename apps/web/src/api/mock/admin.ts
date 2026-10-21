import type { KeeperApi } from '../client';
import { notFound, stateError } from '../errors';
import type { ApiKey, ApiKeyCreated, AuditEntry, Principal } from '../admin-types';
import type { Page } from '../types';
import { buildAdminSeed, type AdminSeed, type DomainAdminData } from './sample/admin';
import { decodeCursor, encodeCursor } from './engine';

export interface AuditInput {
  actor: string;
  action: string;
  target: string | null;
  details?: Record<string, unknown>;
}

export interface AdminMock {
  api: KeeperApi['admin'];
  /** Appends an entry to the audit log of a Domain, as Keeper does for every write. */
  audit(domain: string, input: AuditInput): void;
}

/** Deterministic stand-in for the chained entry hash: 64 hex digits from the previous hash. */
function chainHash(previous: string, entry: AuditEntry): string {
  const { seq, at, domain_id, actor, action, target, details } = entry;
  const text = `${previous}|${JSON.stringify([seq, at, domain_id, actor, action, target, details])}`;
  let hash = 0x811c9dc5;
  let out = '';
  for (let round = 0; round < 8; round += 1) {
    for (let i = 0; i < text.length; i += 1) {
      hash = Math.imul(hash ^ text.charCodeAt(i), 0x01000193) >>> 0;
    }
    hash = Math.imul(hash ^ round, 0x01000193) >>> 0;
    out += hash.toString(16).padStart(8, '0');
  }
  return out;
}

function page<T>(items: T[], limit: number | undefined, cursor: string | null | undefined) {
  const offset = decodeCursor(cursor);
  if (limit === undefined) return { items: items.slice(offset), next_cursor: null } as Page<T>;
  const next = offset + limit < items.length ? encodeCursor(offset + limit) : null;
  return { items: items.slice(offset, offset + limit), next_cursor: next } as Page<T>;
}

/** Keys, principals, grants and the audit log, kept in memory for the page lifetime. */
export function createAdminMock(
  domainExists: (domain: string) => boolean,
  seed: AdminSeed = buildAdminSeed(),
): AdminMock {
  const principals: Principal[] = seed.principals;
  const data = new Map<string, DomainAdminData>(Object.entries(seed.domains));
  let counter = 100;

  const chain = (list: AuditEntry[]) => {
    let previous = '';
    for (const item of list) {
      item.entry_hash = chainHash(previous, item);
      previous = item.entry_hash;
    }
  };
  for (const domain of data.values()) chain(domain.audit);

  const of = (domain: string): DomainAdminData => {
    if (!domainExists(domain)) throw notFound(`Domain ${domain}`);
    let found = data.get(domain);
    if (found === undefined) {
      found = { keys: [], grants: [], audit: [] };
      data.set(domain, found);
    }
    return found;
  };

  const audit = (domain: string, input: AuditInput) => {
    const log = of(domain).audit;
    const last = log[log.length - 1];
    const base: AuditEntry = {
      seq: (last?.seq ?? 0) + 1,
      at: new Date().toISOString(),
      domain_id: domain,
      actor: input.actor,
      action: input.action,
      target: input.target,
      details: input.details ?? {},
      entry_hash: '',
    };
    base.entry_hash = chainHash(last?.entry_hash ?? '', base);
    log.push(base);
  };

  const api: KeeperApi['admin'] = {
    apiKeys: {
      list: (domain, params) => {
        const keys = [...of(domain).keys].sort((a, b) => b.created_at.localeCompare(a.created_at));
        return Promise.resolve(page(keys, params?.limit, params?.cursor));
      },
      create: (domain, input) => {
        const scope = of(domain);
        counter += 1;
        const now = new Date();
        let principalId = input.principal_id;
        if (principalId === undefined) {
          principalId = `svc_${counter.toString()}`;
          principals.push({
            id: principalId,
            type: 'service',
            display_name: input.name,
            external_subject: null,
            created_at: now.toISOString(),
          });
          scope.grants.push({
            id: `grt_${counter.toString()}`,
            domain_id: domain,
            subject: principalId,
            role: input.role ?? 'agent-reader',
            scope_type: 'domain',
            scope_ref: null,
            created_by: 'usr_mock',
            created_at: now.toISOString(),
          });
        } else if (!principals.some((p) => p.id === principalId && p.type === 'service')) {
          return Promise.reject(notFound(`Service principal ${principalId}`));
        }
        const prefix = `lbk_${counter.toString(36).padStart(4, '0')}`;
        const key: ApiKey = {
          id: `key_${counter.toString()}`,
          principal_id: principalId,
          prefix,
          scopes: [`domain:${domain}`],
          expires_at:
            input.ttl_days === undefined
              ? null
              : new Date(now.getTime() + input.ttl_days * 86_400_000).toISOString(),
          last_used_at: null,
          revoked_at: null,
          created_at: now.toISOString(),
        };
        scope.keys.push(key);
        audit(domain, {
          actor: 'usr_mock',
          action: 'apikey.create',
          target: key.id,
          details: { principal_id: principalId, prefix, role: input.role ?? null },
        });
        const created: ApiKeyCreated = {
          ...key,
          key: `${prefix}_mock_${'0123456789abcdef'.repeat(2)}`,
          role: input.principal_id === undefined ? (input.role ?? 'agent-reader') : null,
        };
        return Promise.resolve(created);
      },
      revoke: (domain, keyId) => {
        const key = of(domain).keys.find((k) => k.id === keyId);
        if (key === undefined) return Promise.reject(notFound(`API key ${keyId}`));
        if (key.revoked_at !== null) {
          return Promise.reject(stateError(`API key ${keyId} is revoked.`, 'key_revoked'));
        }
        key.revoked_at = new Date().toISOString();
        audit(domain, {
          actor: 'usr_mock',
          action: 'apikey.revoke',
          target: keyId,
          details: { principal_id: key.principal_id },
        });
        return Promise.resolve();
      },
    },
    principals: {
      list: (params) => {
        const list = principals.filter((p) => params?.type === undefined || p.type === params.type);
        return Promise.resolve(page(list, params?.limit, params?.cursor));
      },
    },
    grants: {
      list: (domain, params) =>
        Promise.resolve(page(of(domain).grants, params?.limit, params?.cursor)),
    },
    audit: {
      list: (domain, params) => {
        const newestFirst = [...of(domain).audit].reverse();
        return Promise.resolve(page(newestFirst, params?.limit, params?.cursor));
      },
    },
  };

  return { api, audit };
}
