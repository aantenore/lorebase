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
  /** Prefix of the Branch the Modeler creates to carry schema changes of a protected Branch. */
  schemaBranchPrefix: string;
  /** "Start from data" in the Modeler: upload caps, accepted suffixes and the review defaults. */
  inference: InferenceConfig;
}

export interface InferenceConfig {
  /** Files per request; Keeper's `keeper.schema_inference.max_files`. */
  maxFiles: number;
  /** Bytes per file; Keeper's `max_file_bytes`. */
  maxFileBytes: number;
  /** Bytes for all files together; Keeper's `max_total_bytes`. */
  maxTotalBytes: number;
  /** Lower-case file suffixes the upload accepts, with the dot. */
  fileSuffixes: string[];
  /** Confidence (0 to 1) from which "Accept all above" accepts an element. */
  acceptThreshold: number;
  /** Secret references offered as suggestions for Postgres sources (Keeper's allowlist). */
  postgresSources: string[];
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
  schemaBranchPrefix: 'schema/',
  // The caps repeat the defaults of keeper.schema_inference so a stock Keeper and a stock app agree.
  inferMaxFiles: 50,
  inferMaxFileBytes: 1_048_576,
  inferMaxTotalBytes: 1_048_576,
  inferFileSuffixes: ['.csv', '.tsv', '.txt'],
  inferAcceptThreshold: 0.9,
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

function list(raw: RawEnv, key: string): string[] | undefined {
  const value = text(raw, key);
  if (value === undefined) return undefined;
  return value
    .split(',')
    .map((part) => part.trim())
    .filter((part) => part !== '');
}

function fraction(raw: RawEnv, key: string, fallback: number): number {
  const value = text(raw, key);
  if (value === undefined) return fallback;
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0 || parsed > 1) {
    throw new ConfigError(`${key} must be a number between 0 and 1, got "${value}".`);
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
    schemaBranchPrefix:
      text(raw, 'VITE_SCHEMA_BRANCH_PREFIX') ?? NON_FUNCTIONAL_DEFAULTS.schemaBranchPrefix,
    inference: {
      maxFiles: positiveInt(raw, 'VITE_INFER_MAX_FILES', NON_FUNCTIONAL_DEFAULTS.inferMaxFiles),
      maxFileBytes: positiveInt(
        raw,
        'VITE_INFER_MAX_FILE_BYTES',
        NON_FUNCTIONAL_DEFAULTS.inferMaxFileBytes,
      ),
      maxTotalBytes: positiveInt(
        raw,
        'VITE_INFER_MAX_TOTAL_BYTES',
        NON_FUNCTIONAL_DEFAULTS.inferMaxTotalBytes,
      ),
      fileSuffixes: (
        list(raw, 'VITE_INFER_FILE_SUFFIXES') ?? [...NON_FUNCTIONAL_DEFAULTS.inferFileSuffixes]
      ).map((suffix) => suffix.toLowerCase()),
      acceptThreshold: fraction(
        raw,
        'VITE_INFER_ACCEPT_THRESHOLD',
        NON_FUNCTIONAL_DEFAULTS.inferAcceptThreshold,
      ),
      postgresSources: list(raw, 'VITE_INFER_POSTGRES_SOURCES') ?? [],
    },
  };
}

/** Configuration for tests and stories: mock mode with default tunables. */
export function testConfig(overrides: Partial<AppConfig> = {}): AppConfig {
  return { ...loadConfig({ VITE_API_MODE: 'mock' }), ...overrides };
}
