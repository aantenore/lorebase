/** Administration types (docs/openapi.json: api-keys, principals, grants, audit). */

export type PrincipalType = 'user' | 'service' | 'api_key';

export interface Principal {
  id: string;
  type: PrincipalType;
  display_name: string;
  external_subject: string | null;
  created_at: string;
}

export interface ApiKey {
  id: string;
  principal_id: string;
  /** First characters of the key, to recognize it. The secret is never shown again. */
  prefix: string;
  scopes: string[];
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

/** The answer of key creation; `key` is the only time the secret is returned. */
export interface ApiKeyCreated extends ApiKey {
  key: string;
  role?: string | null;
}

export interface CreateApiKeyInput {
  /** Name of the service principal. */
  name: string;
  role?: string;
  /** Lifetime in days; Keeper's default when omitted. */
  ttl_days?: number;
  /** Add the key to an existing service principal instead of creating one. */
  principal_id?: string;
}

export type GrantScopeType = 'domain' | 'kind' | 'operation' | 'branch';

export interface Grant {
  id: string;
  domain_id: string;
  /** Principal or group id. */
  subject: string;
  role: string;
  scope_type: GrantScopeType;
  scope_ref: string | null;
  created_by: string;
  created_at: string;
}

export interface AuditEntry {
  seq: number;
  at: string;
  domain_id: string | null;
  actor: string;
  action: string;
  target: string | null;
  details: Record<string, unknown>;
  entry_hash: string;
}

export interface PrincipalListParams {
  type?: PrincipalType;
  limit?: number;
  cursor?: string | null;
}
