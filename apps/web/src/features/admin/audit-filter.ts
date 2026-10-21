import type { AuditEntry } from '@/api/admin-types';

export interface AuditFilter {
  /** Exact actor id; empty for all. */
  actor: string;
  /** Exact action, for example `apikey.revoke`; empty for all. */
  action: string;
  /** Case-insensitive text found in the target, the actor or the details. */
  text: string;
}

export const NO_FILTER: AuditFilter = { actor: '', action: '', text: '' };

/** Keeper pages the audit log and filters nothing, so the screen filters what it has loaded. */
export function filterAudit(entries: readonly AuditEntry[], filter: AuditFilter): AuditEntry[] {
  const needle = filter.text.trim().toLowerCase();
  return entries.filter((entry) => {
    if (filter.actor !== '' && entry.actor !== filter.actor) return false;
    if (filter.action !== '' && entry.action !== filter.action) return false;
    if (needle === '') return true;
    const haystack = `${entry.actor} ${entry.action} ${entry.target ?? ''} ${JSON.stringify(entry.details)}`;
    return haystack.toLowerCase().includes(needle);
  });
}

/** Distinct values of one field, sorted, for the filter menus. */
export function distinct(entries: readonly AuditEntry[], field: 'actor' | 'action'): string[] {
  return [...new Set(entries.map((entry) => entry[field]))].sort();
}
