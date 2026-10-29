import type { AppConfig } from '@/config/env';
import type { KeeperApi } from './client';
import type { Credentials } from './credentials';
import { createHttpApi } from './http';
import { asRemote, createMockApi } from './mock/adapter';
import { redirectToLogin, type SessionClient } from './session';

export type { KeeperApi } from './client';
export { createCredentials, type Credentials } from './credentials';
export { createSessionClient, type SessionClient, type SessionInfo } from './session';
export {
  ApiError,
  ApprovalsRequiredError,
  ConflictError,
  ForbiddenError,
  MergeConflictsError,
  NetworkError,
  NotFoundError,
  ProtectedBranchError,
  UnauthorizedError,
  ValidationError,
  describeError,
  isApiError,
} from './errors';
export * from './types';
export * from './admin-types';
export * from './extraction-types';
export * from './inference-types';
export * from './sources-types';
export * from './suggestion-types';
export * from './reconciliation-types';

/**
 * Pick the adapter from configuration: `live` talks to Keeper, `mock` serves sample data.
 * The live adapter reads the token from `credentials` on every request; in session mode it
 * sends the session cookie and the CSRF header of `session` instead.
 */
export function createKeeperApi(
  config: AppConfig,
  credentials: Credentials,
  session: SessionClient | null = null,
): KeeperApi {
  if (config.apiMode === 'live') {
    if (config.keeperBaseUrl === null) throw new Error('Live mode needs a Keeper base URL.');
    if (config.authMode === 'session') {
      if (session === null) throw new Error('Session mode needs a session client.');
      return createHttpApi({
        baseUrl: config.keeperBaseUrl,
        credentials: 'include',
        headers: (method) => session.csrfHeaders(method),
        onCsrfRejected: async () => (await session.load()) !== null,
        onUnauthorized: redirectToLogin,
      });
    }
    return createHttpApi({
      baseUrl: config.keeperBaseUrl,
      headers: (): Record<string, string> => {
        const token = credentials.token();
        return token === null ? {} : { Authorization: `Bearer ${token}` };
      },
    });
  }
  return asRemote(
    createMockApi({
      pageSize: config.pageSize,
      searchLimit: config.searchLimit,
      maxNodes: config.canvas.maxNodes,
      perfGraph: config.mockPerfGraph,
    }),
    config.mockLatencyMs,
  );
}
