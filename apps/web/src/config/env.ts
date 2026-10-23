/**
 * Runtime configuration. Values come from Vite environment variables (see `.env.example`),
 * overlaid at start by `window.__LOREBASE_CONFIG__` (served as `/config.js`).
 * Only non-functional tunables (page sizes, cache lifetimes) have defaults in code;
 * the data source and the Keeper endpoint must be provided by the environment.
 */

declare global {
  interface Window {
    /** Set by `/config.js` before the app starts; validated by `withRuntimeConfig`. */
    __LOREBASE_CONFIG__?: unknown;
  }
}

export type ApiMode = 'mock' | 'live';

/**
 * How a live app authenticates: `token` sends a Bearer token, `session` relies on the cookie
 * session Keeper opens after an OIDC login (Backend-for-Frontend).
 */
export type AuthMode = 'token' | 'session';

export interface AppConfig {
  apiMode: ApiMode;
  /** Keeper origin without the API path. `null` in mock mode. */
  keeperBaseUrl: string | null;
  /**
   * Guide origin (`VITE_GUIDE_URL`, `guideUrl` in `/config.js`), without a trailing slash. `null`
   * when the assistant is not configured. The Retrieval lab then asks `POST {guideUrl}/embed`
   * for the query vector (request `{"text": string}`, answer `{"vector": number[], "model"?:
   * string, "dims"?: number}`); without it the lab searches by words only, or takes a vector
   * pasted by hand. The Explore panel streams `POST {guideUrl}/explore`; in mock mode it plays a
   * scripted exploration instead, whatever this value.
   */
  guideUrl: string | null;
  /**
   * Base of the documentation site (`VITE_DOCS_URL`, `docsUrl` in `/config.js`) that "Learn
   * more" links point into, without a trailing slash. `VITE_DOCS_URL=none` hides those links.
   */
  docsUrl: string | null;
  /** From `VITE_AUTH_MODE`. Always `token` in mock mode. */
  authMode: AuthMode;
  /**
   * Token sent as `Authorization: Bearer` in live mode, from `VITE_API_TOKEN`. Anything in a
   * `VITE_` variable is readable in the bundle: development only. `null` in session mode.
   */
  apiToken: string | null;
  pageSize: number;
  searchLimit: number;
  canvas: CanvasConfig;
  /** Mock API only: serve a generated graph of this size from every neighborhood query. */
  mockPerfGraph: { nodes: number; edges: number } | null;
  queryStaleMs: number;
  toastMs: number;
  mockLatencyMs: number;
  /** Prefix of the Branch the Modeler creates to carry schema changes of a protected Branch. */
  schemaBranchPrefix: string;
  /** "Start from data" in the Modeler: upload caps, accepted suffixes and the review defaults. */
  inference: InferenceConfig;
  /** Documents screen: upload caps, accepted suffixes and the extraction defaults. */
  documents: DocumentsConfig;
  /** Extraction item review in Review. */
  extraction: ExtractionConfig;
  /** The Explore panel (questions to Guide). */
  explore: ExploreConfig;
}

export interface CanvasConfig {
  depth: number;
  maxNodes: number;
  /** ForceAtlas2 iterations run on the main thread before the first draw (small graphs only). */
  layoutIterations: number;
  /** Largest graph that is pre-laid out on the main thread; bigger ones start unsettled. */
  prelayoutMaxNodes: number;
  /** Web worker layout: how long it may run and when it counts as settled. */
  layout: {
    /** Hard stop for a layout run, in milliseconds. */
    timeBudgetMs: number;
    /** Interval between two convergence samples, in milliseconds. */
    sampleMs: number;
    /** Mean node move per sample, as a fraction of the graph extent, under which it is settled. */
    convergence: number;
    /** Consecutive settled samples needed before the layout stops. */
    stableSamples: number;
    /** From this many nodes ForceAtlas2 uses the Barnes-Hut approximation. */
    barnesHutMinNodes: number;
  };
  /** Thinning of what is drawn as the graph grows. */
  detail: {
    /** Above this many nodes only the larger nodes get a label. */
    labelsMaxNodes: number;
    /** Node size (pixels) a node needs to get a label in a dense graph. */
    denseLabelMinSize: number;
    /** Above this many edges edge labels are not drawn. */
    edgeLabelsMaxEdges: number;
    /** From this many edges they are hidden while the view moves. */
    hideEdgesOnMoveMinEdges: number;
    /** Edges listed in the textual list view; the rest is summarized. */
    listMaxEdges: number;
  };
}

export interface DocumentsConfig {
  /** Documents per drop; the upload sends one request per document. */
  maxFiles: number;
  /** Bytes per document; Keeper's `keeper.documents.max_bytes`. */
  maxFileBytes: number;
  /** Lower-case file suffixes the upload accepts, with the dot; Keeper's `media_types`. */
  fileSuffixes: string[];
  /** Tiers preselected for an extraction (0 rules, 1 local models, 2 chat model). */
  defaultTiers: number[];
  /** Whether an upload queues an extraction unless the person turns it off. */
  extractOnUpload: boolean;
  /** Offer intake by URL; Keeper allows it only for hosts in `keeper.documents.url_hosts`. */
  urlIntake: boolean;
  /** How often the library asks again while an extraction job is queued or running. */
  jobPollMs: number;
}

export interface ExtractionConfig {
  /** Confidence (0 to 1) from which "Accept at or above" accepts the matching items. */
  acceptThreshold: number;
  /** Items loaded per page of the item list. */
  itemsPageSize: number;
}

export interface ExploreConfig {
  /** Steps preset in the panel; Guide caps them at `guide.agent.max_steps`. */
  maxSteps: number;
  /** The panel stops an exploration that streams for longer than this, in milliseconds. */
  timeoutMs: number;
  /** Mock API only: pause between two scripted events, in milliseconds. */
  mockStepMs: number;
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
  canvasPrelayoutMaxNodes: 500,
  canvasLayoutTimeBudgetMs: 20_000,
  canvasLayoutSampleMs: 500,
  canvasConvergence: 0.01,
  canvasStableSamples: 3,
  canvasBarnesHutMinNodes: 1000,
  canvasLabelsMaxNodes: 300,
  canvasDenseLabelMinSize: 14,
  canvasEdgeLabelsMaxEdges: 300,
  canvasHideEdgesOnMoveMinEdges: 2000,
  canvasListMaxEdges: 500,
  queryStaleMs: 30_000,
  toastMs: 5000,
  mockLatencyMs: 0,
  schemaBranchPrefix: 'schema/',
  docsUrl: 'https://aantenore.github.io/lorebase',
  // The caps repeat the defaults of keeper.schema_inference so a stock Keeper and a stock app agree.
  inferMaxFiles: 50,
  inferMaxFileBytes: 1_048_576,
  inferMaxTotalBytes: 1_048_576,
  inferFileSuffixes: ['.csv', '.tsv', '.txt', '.json', '.jsonl', '.ndjson', '.parquet'],
  inferAcceptThreshold: 0.9,
  // The document caps repeat the defaults of keeper.documents.
  documentsMaxFiles: 20,
  documentsMaxFileBytes: 20_971_520,
  documentsFileSuffixes: ['.md', '.markdown', '.txt', '.pdf', '.docx'],
  documentsTiers: [0, 1],
  documentsJobPollMs: 3000,
  extractionAcceptThreshold: 0.9,
  extractionItemsPageSize: 200,
  exploreMaxSteps: 6,
  // Above guide.agent.timeout_s (120 s), so Guide's own timeout normally ends the stream first.
  exploreTimeoutMs: 150_000,
  exploreMockStepMs: 350,
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

function flag(raw: RawEnv, key: string, fallback: boolean): boolean {
  const value = text(raw, key);
  if (value === undefined) return fallback;
  if (value === 'true' || value === 'false') return value === 'true';
  throw new ConfigError(`${key} must be "true" or "false", got "${value}".`);
}

function tiers(raw: RawEnv, key: string, fallback: readonly number[]): number[] {
  const values = list(raw, key);
  if (values === undefined) return [...fallback];
  const parsed = values.map(Number);
  if (parsed.length === 0 || !parsed.every((t) => t === 0 || t === 1 || t === 2)) {
    throw new ConfigError(`${key} must list tiers among 0, 1 and 2, got "${values.join(',')}".`);
  }
  return [...new Set(parsed)].sort((a, b) => a - b);
}

/** `VITE_MOCK_PERF_NODES` and `VITE_MOCK_PERF_EDGES` together switch on the generated graph. */
function mockPerfGraph(raw: RawEnv): AppConfig['mockPerfGraph'] {
  const nodes = text(raw, 'VITE_MOCK_PERF_NODES');
  if (nodes === undefined) return null;
  return {
    nodes: positiveInt(raw, 'VITE_MOCK_PERF_NODES', 0, 2),
    edges: positiveInt(raw, 'VITE_MOCK_PERF_EDGES', Number(nodes) * 2, 0),
  };
}

function parseBaseUrl(value: string, key = 'VITE_KEEPER_BASE_URL'): string {
  // A path on the app's own origin, for a deployment that proxies /api/v1 to Keeper.
  if (value.startsWith('/')) return value.replace(/\/+$/, '');
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new ConfigError(`${key} is not a valid URL or path: "${value}".`);
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new ConfigError(`${key} must use http or https.`);
  }
  return url.origin + url.pathname.replace(/\/+$/, '');
}

/** Friendly runtime keys and the variable each one stands for. */
const RUNTIME_ALIASES: Readonly<Record<string, string>> = {
  apiMode: 'VITE_API_MODE',
  keeperBaseUrl: 'VITE_KEEPER_BASE_URL',
  guideUrl: 'VITE_GUIDE_URL',
  docsUrl: 'VITE_DOCS_URL',
  authMode: 'VITE_AUTH_MODE',
  apiToken: 'VITE_API_TOKEN',
  token: 'VITE_API_TOKEN',
};
const RUNTIME_VARIABLE = /^VITE_[A-Z0-9_]+$/;

/**
 * The build environment overlaid with the runtime object of `/config.js`. Runtime wins; only
 * non-empty strings and finite numbers count, everything else leaves the build value in place.
 */
export function withRuntimeConfig(buildEnv: RawEnv, runtime: unknown): RawEnv {
  if (typeof runtime !== 'object' || runtime === null || Array.isArray(runtime)) return buildEnv;
  const merged: Record<string, string | boolean | undefined> = { ...buildEnv };
  for (const [key, value] of Object.entries(runtime)) {
    const name = Object.hasOwn(RUNTIME_ALIASES, key)
      ? RUNTIME_ALIASES[key]
      : RUNTIME_VARIABLE.test(key)
        ? key
        : undefined;
    if (name === undefined) continue;
    const str =
      typeof value === 'string'
        ? value.trim()
        : typeof value === 'number' && Number.isFinite(value)
          ? String(value)
          : '';
    if (str !== '') merged[name] = str;
  }
  return merged;
}

export function loadConfig(raw: RawEnv): AppConfig {
  const mode = text(raw, 'VITE_API_MODE');
  if (mode !== 'mock' && mode !== 'live') {
    throw new ConfigError(
      'VITE_API_MODE (apiMode in /config.js) must be "mock" or "live". See .env.example.',
    );
  }

  let keeperBaseUrl: string | null = null;
  if (mode === 'live') {
    const base = text(raw, 'VITE_KEEPER_BASE_URL');
    if (base === undefined) {
      throw new ConfigError('VITE_KEEPER_BASE_URL is required when VITE_API_MODE=live.');
    }
    keeperBaseUrl = parseBaseUrl(base);
  }

  const guideText = text(raw, 'VITE_GUIDE_URL');
  const guideUrl = guideText === undefined ? null : parseBaseUrl(guideText, 'VITE_GUIDE_URL');

  const docsText = text(raw, 'VITE_DOCS_URL') ?? NON_FUNCTIONAL_DEFAULTS.docsUrl;
  const docsUrl = docsText === 'none' ? null : parseBaseUrl(docsText, 'VITE_DOCS_URL');

  const authText = text(raw, 'VITE_AUTH_MODE') ?? 'token';
  if (authText !== 'token' && authText !== 'session') {
    throw new ConfigError(
      `VITE_AUTH_MODE (authMode in /config.js) must be "token" or "session", got "${authText}".`,
    );
  }
  const authMode: AuthMode = mode === 'live' ? authText : 'token';

  return {
    apiMode: mode,
    keeperBaseUrl,
    guideUrl,
    docsUrl,
    authMode,
    apiToken:
      mode === 'live' && authMode === 'token' ? (text(raw, 'VITE_API_TOKEN') ?? null) : null,
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
      prelayoutMaxNodes: positiveInt(
        raw,
        'VITE_CANVAS_PRELAYOUT_MAX_NODES',
        NON_FUNCTIONAL_DEFAULTS.canvasPrelayoutMaxNodes,
        0,
      ),
      layout: {
        timeBudgetMs: positiveInt(
          raw,
          'VITE_CANVAS_LAYOUT_TIME_BUDGET_MS',
          NON_FUNCTIONAL_DEFAULTS.canvasLayoutTimeBudgetMs,
        ),
        sampleMs: positiveInt(
          raw,
          'VITE_CANVAS_LAYOUT_SAMPLE_MS',
          NON_FUNCTIONAL_DEFAULTS.canvasLayoutSampleMs,
        ),
        convergence: fraction(
          raw,
          'VITE_CANVAS_LAYOUT_CONVERGENCE',
          NON_FUNCTIONAL_DEFAULTS.canvasConvergence,
        ),
        stableSamples: positiveInt(
          raw,
          'VITE_CANVAS_LAYOUT_STABLE_SAMPLES',
          NON_FUNCTIONAL_DEFAULTS.canvasStableSamples,
        ),
        barnesHutMinNodes: positiveInt(
          raw,
          'VITE_CANVAS_BARNES_HUT_MIN_NODES',
          NON_FUNCTIONAL_DEFAULTS.canvasBarnesHutMinNodes,
        ),
      },
      detail: {
        labelsMaxNodes: positiveInt(
          raw,
          'VITE_CANVAS_LABELS_MAX_NODES',
          NON_FUNCTIONAL_DEFAULTS.canvasLabelsMaxNodes,
          0,
        ),
        denseLabelMinSize: positiveInt(
          raw,
          'VITE_CANVAS_DENSE_LABEL_MIN_SIZE',
          NON_FUNCTIONAL_DEFAULTS.canvasDenseLabelMinSize,
        ),
        edgeLabelsMaxEdges: positiveInt(
          raw,
          'VITE_CANVAS_EDGE_LABELS_MAX_EDGES',
          NON_FUNCTIONAL_DEFAULTS.canvasEdgeLabelsMaxEdges,
          0,
        ),
        hideEdgesOnMoveMinEdges: positiveInt(
          raw,
          'VITE_CANVAS_HIDE_EDGES_ON_MOVE_MIN_EDGES',
          NON_FUNCTIONAL_DEFAULTS.canvasHideEdgesOnMoveMinEdges,
        ),
        listMaxEdges: positiveInt(
          raw,
          'VITE_CANVAS_LIST_MAX_EDGES',
          NON_FUNCTIONAL_DEFAULTS.canvasListMaxEdges,
          0,
        ),
      },
    },
    mockPerfGraph: mockPerfGraph(raw),
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
    documents: {
      maxFiles: positiveInt(
        raw,
        'VITE_DOCUMENTS_MAX_FILES',
        NON_FUNCTIONAL_DEFAULTS.documentsMaxFiles,
      ),
      maxFileBytes: positiveInt(
        raw,
        'VITE_DOCUMENTS_MAX_FILE_BYTES',
        NON_FUNCTIONAL_DEFAULTS.documentsMaxFileBytes,
      ),
      fileSuffixes: (
        list(raw, 'VITE_DOCUMENTS_FILE_SUFFIXES') ?? [
          ...NON_FUNCTIONAL_DEFAULTS.documentsFileSuffixes,
        ]
      ).map((suffix) => suffix.toLowerCase()),
      defaultTiers: tiers(raw, 'VITE_DOCUMENTS_TIERS', NON_FUNCTIONAL_DEFAULTS.documentsTiers),
      extractOnUpload: flag(raw, 'VITE_DOCUMENTS_EXTRACT_ON_UPLOAD', true),
      urlIntake: flag(raw, 'VITE_DOCUMENTS_URL_INTAKE', true),
      jobPollMs: positiveInt(
        raw,
        'VITE_DOCUMENTS_JOB_POLL_MS',
        NON_FUNCTIONAL_DEFAULTS.documentsJobPollMs,
        0,
      ),
    },
    extraction: {
      acceptThreshold: fraction(
        raw,
        'VITE_EXTRACTION_ACCEPT_THRESHOLD',
        NON_FUNCTIONAL_DEFAULTS.extractionAcceptThreshold,
      ),
      itemsPageSize: positiveInt(
        raw,
        'VITE_EXTRACTION_ITEMS_PAGE_SIZE',
        NON_FUNCTIONAL_DEFAULTS.extractionItemsPageSize,
      ),
    },
    explore: {
      maxSteps: positiveInt(raw, 'VITE_EXPLORE_MAX_STEPS', NON_FUNCTIONAL_DEFAULTS.exploreMaxSteps),
      timeoutMs: positiveInt(
        raw,
        'VITE_EXPLORE_TIMEOUT_MS',
        NON_FUNCTIONAL_DEFAULTS.exploreTimeoutMs,
      ),
      mockStepMs: positiveInt(
        raw,
        'VITE_EXPLORE_MOCK_STEP_MS',
        NON_FUNCTIONAL_DEFAULTS.exploreMockStepMs,
        0,
      ),
    },
  };
}

/** Configuration for tests and stories: mock mode with default tunables. */
export function testConfig(overrides: Partial<AppConfig> = {}): AppConfig {
  return { ...loadConfig({ VITE_API_MODE: 'mock' }), ...overrides };
}
