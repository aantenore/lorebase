import type { AppConfig } from '@/config/env';
import type { KeeperApi } from './client';
import type { Credentials } from './credentials';
import { createHttpApi } from './http';
import { asRemote, createMockApi } from './mock/adapter';

export type { KeeperApi } from './client';
export { createCredentials, type Credentials } from './credentials';
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
export * from './inference-types';
export * from './sources-types';

/**
 * Pick the adapter from configuration: `live` talks to Keeper, `mock` serves sample data.
 * The live adapter reads the token from `credentials` on every request.
 */
export function createKeeperApi(config: AppConfig, credentials: Credentials): KeeperApi {
  if (config.apiMode === 'live') {
    if (config.keeperBaseUrl === null) throw new Error('Live mode needs a Keeper base URL.');
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
    }),
    config.mockLatencyMs,
  );
}
