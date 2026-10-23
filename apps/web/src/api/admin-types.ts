/**
 * Administration types (docs/openapi.json: api-keys, principals, grants, audit). Each one is
 * the generated contract type (`./generated/keeper.ts`) under the name the screens use.
 */
import type { components } from './generated/keeper';

type Schemas = components['schemas'];

export type PrincipalType = Schemas['PrincipalType'];
export type Principal = Schemas['PrincipalOut'];
/** `prefix` is the first characters of the key, to recognize it; the secret is never shown again. */
export type ApiKey = Schemas['ApiKeyOut'];
/** The answer of key creation; `key` is the only time the secret is returned. */
export type ApiKeyCreated = Schemas['ApiKeyCreated'];
/** `name` names the service principal; `principal_id` adds the key to an existing one. */
export type CreateApiKeyInput = Schemas['CreateApiKey'];
export type GrantScopeType = Schemas['GrantScope'];
/** `subject` is a principal or group id. */
export type Grant = Schemas['GrantOut'];
export type AuditEntry = Schemas['AuditEntryOut'];

export interface PrincipalListParams {
  type?: PrincipalType;
  limit?: number;
  cursor?: string | null;
}
