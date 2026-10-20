/**
 * Runtime configuration. Values come from Vite environment variables (see `.env.example`).
 * Only non-functional tunables (page sizes, cache lifetimes) have defaults in code;
 * the data source and the Keeper endpoint must be provided by the environment.
 */

export type ApiMode = 'mock' | 'live';

export interface AppConfig {
  apiMode: ApiMode;
  /** Keeper origin without the API path. `null` in mock mode. */
  keeperBaseUrl: string | null;
  pageSize: number;
  searchLimit: number;
  canvas: { depth: number; maxNodes: number; layoutIterations: number };
  queryStaleMs: number;
  toastMs: number;
  mockLatencyMs: number;
}

export type RawEnv = Readonly<Record<string, string | boolean | undefined>>;

export class ConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ConfigError';
  }
}

const NON_FUNCTIONAL_DEFAULTS = {
  pageSize: 50,
  searchLimit: 20,
  canvasDepth: 2,
  canvasMaxNodes: 200,
  canvasLayoutIterations: 100,
  queryStaleMs: 30_000,
  toastMs: 5000,
  mockLatencyMs: 0,
} as const;

function text(raw: RawEnv, key: string): string | undefined {
  const value = raw[key];
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : undefined;
}

function positiveInt(raw: RawEnv, key: string, fallback: number, min = 1): number {
  const value = text(raw, key);
  if (value === undefined) return fallback;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < min) {
    throw new ConfigError(`${key} must be an integer >= ${min}, got "${value}".`);
  }
  return parsed;
}

function parseBaseUrl(value: string): string {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new ConfigError(`VITE_KEEPER_BASE_URL is not a valid URL: "${value}".`);
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new ConfigError('VITE_KEEPER_BASE_URL must use http or https.');
  }
  return url.origin + url.pathname.replace(/\/+$/, '');
}

export function loadConfig(raw: RawEnv): AppConfig {
  const mode = text(raw, 'VITE_API_MODE');
  if (mode !== 'mock' && mode !== 'live') {
    throw new ConfigError('VITE_API_MODE must be "mock" or "live". See .env.example.');
  }

  let keeperBaseUrl: string | null = null;
  if (mode === 'live') {
    const base = text(raw, 'VITE_KEEPER_BASE_URL');
    if (base === undefined) {
      throw new ConfigError('VITE_KEEPER_BASE_URL is required when VITE_API_MODE=live.');
    }
    keeperBaseUrl = parseBaseUrl(base);
  }

  return {
    apiMode: mode,
    keeperBaseUrl,
    pageSize: positiveInt(raw, 'VITE_PAGE_SIZE', NON_FUNCTIONAL_DEFAULTS.pageSize),
    searchLimit: positiveInt(raw, 'VITE_SEARCH_LIMIT', NON_FUNCTIONAL_DEFAULTS.searchLimit),
    canvas: {
      depth: positiveInt(raw, 'VITE_CANVAS_DEPTH', NON_FUNCTIONAL_DEFAULTS.canvasDepth),
      maxNodes: positiveInt(raw, 'VITE_CANVAS_MAX_NODES', NON_FUNCTIONAL_DEFAULTS.canvasMaxNodes),
      layoutIterations: positiveInt(
        raw,
        'VITE_CANVAS_LAYOUT_ITERATIONS',
        NON_FUNCTIONAL_DEFAULTS.canvasLayoutIterations,
      ),
    },
    queryStaleMs: positiveInt(raw, 'VITE_QUERY_STALE_MS', NON_FUNCTIONAL_DEFAULTS.queryStaleMs, 0),
    toastMs: positiveInt(raw, 'VITE_TOAST_MS', NON_FUNCTIONAL_DEFAULTS.toastMs),
    mockLatencyMs: positiveInt(
      raw,
      'VITE_MOCK_LATENCY_MS',
      NON_FUNCTIONAL_DEFAULTS.mockLatencyMs,
      0,
    ),
  };
}

/** Configuration for tests and stories: mock mode with default tunables. */
export function testConfig(overrides: Partial<AppConfig> = {}): AppConfig {
  return { ...loadConfig({ VITE_API_MODE: 'mock' }), ...overrides };
}
