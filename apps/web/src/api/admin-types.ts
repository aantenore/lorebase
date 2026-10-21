/**
 * Administration types (docs/openapi.json: api-keys, principals, grants, audit). Each one is
 * the generated contract type (`./generated/keeper.ts`) under the name the screens use.
 */
import type { components } from './generated/keeper';

type Contract = components['schemas'];

export type PrincipalType = Contract['PrincipalType'];
export type Principal = Contract['PrincipalOut'];
/** `prefix` is the first characters of the key, to recognize it; the secret is never shown again. */
export type ApiKey = Contract['ApiKeyOut'];
/** The answer of key creation; `key` is the only time the secret is returned. */
export type ApiKeyCreated = Contract['ApiKeyCreated'];
/** `name` names the service principal; `principal_id` adds the key to an existing one. */
export type CreateApiKeyInput = Contract['CreateApiKey'];
export type GrantScopeType = Contract['GrantScope'];
/** `subject` is a principal or group id. */
export type Grant = Contract['GrantOut'];
export type AuditEntry = Contract['AuditEntryOut'];

export interface PrincipalListParams {
  type?: PrincipalType;
  limit?: number;
  cursor?: string | null;
}
