import type { AppConfig } from '@/config/env';
import type { KeeperApi } from './client';
import { createHttpApi } from './http';
import { asRemote, createMockApi } from './mock/adapter';

export type { KeeperApi } from './client';
export { ApiError, isApiError } from './errors';
export * from './types';

/** Pick the adapter from configuration: `live` talks to Keeper, `mock` serves sample data. */
export function createKeeperApi(config: AppConfig): KeeperApi {
  if (config.apiMode === 'live') {
    if (config.keeperBaseUrl === null) throw new Error('Live mode needs a Keeper base URL.');
    return createHttpApi({ baseUrl: config.keeperBaseUrl });
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
