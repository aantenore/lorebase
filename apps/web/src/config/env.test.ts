import { ConfigError, loadConfig, withRuntimeConfig } from './env';

describe('loadConfig', () => {
  it('requires an explicit API mode', () => {
    expect(() => loadConfig({})).toThrow(ConfigError);
    expect(() => loadConfig({ VITE_API_MODE: 'other' })).toThrow(/mock" or "live/);
  });

  it('accepts mock mode without a Keeper URL', () => {
    const config = loadConfig({ VITE_API_MODE: 'mock' });
    expect(config.apiMode).toBe('mock');
    expect(config.keeperBaseUrl).toBeNull();
  });

  it('requires a valid Keeper URL in live mode and normalizes it', () => {
    expect(() => loadConfig({ VITE_API_MODE: 'live' })).toThrow(/VITE_KEEPER_BASE_URL/);
    expect(() => loadConfig({ VITE_API_MODE: 'live', VITE_KEEPER_BASE_URL: 'nope' })).toThrow(
      /valid URL or path/,
    );
    const config = loadConfig({
      VITE_API_MODE: 'live',
      VITE_KEEPER_BASE_URL: 'https://keeper.example.test/',
    });
    expect(config.keeperBaseUrl).toBe('https://keeper.example.test');
  });

  it('takes a path on the app origin as the Keeper URL, for a proxied deployment', () => {
    const config = loadConfig({ VITE_API_MODE: 'live', VITE_KEEPER_BASE_URL: '/' });
    expect(config.keeperBaseUrl).toBe('');
  });

  it('reads the API token only in live mode', () => {
    const live = { VITE_API_MODE: 'live', VITE_KEEPER_BASE_URL: '/', VITE_API_TOKEN: ' lbk_x ' };
    expect(loadConfig(live).apiToken).toBe('lbk_x');
    expect(loadConfig({ ...live, VITE_API_MODE: 'mock' }).apiToken).toBeNull();
    expect(loadConfig({ ...live, VITE_API_TOKEN: '' }).apiToken).toBeNull();
  });

  it('lets the environment override tunables and rejects bad numbers', () => {
    const config = loadConfig({ VITE_API_MODE: 'mock', VITE_PAGE_SIZE: '25' });
    expect(config.pageSize).toBe(25);
    expect(() => loadConfig({ VITE_API_MODE: 'mock', VITE_PAGE_SIZE: '0' })).toThrow(ConfigError);
  });

  it('reads the inference caps, suffixes, threshold and Postgres suggestions', () => {
    const defaults = loadConfig({ VITE_API_MODE: 'mock' }).inference;
    expect(defaults).toMatchObject({ maxFiles: 50, maxFileBytes: 1_048_576, acceptThreshold: 0.9 });
    expect(defaults.fileSuffixes).toEqual(['.csv', '.tsv', '.txt']);
    expect(defaults.postgresSources).toEqual([]);

    const config = loadConfig({
      VITE_API_MODE: 'mock',
      VITE_INFER_MAX_FILES: '5',
      VITE_INFER_MAX_FILE_BYTES: '2048',
      VITE_INFER_MAX_TOTAL_BYTES: '4096',
      VITE_INFER_FILE_SUFFIXES: '.CSV, .psv',
      VITE_INFER_ACCEPT_THRESHOLD: '0.75',
      VITE_INFER_POSTGRES_SOURCES: 'secret://env/A, secret://env/B',
    }).inference;
    expect(config).toEqual({
      maxFiles: 5,
      maxFileBytes: 2048,
      maxTotalBytes: 4096,
      fileSuffixes: ['.csv', '.psv'],
      acceptThreshold: 0.75,
      postgresSources: ['secret://env/A', 'secret://env/B'],
    });
    expect(() => loadConfig({ VITE_API_MODE: 'mock', VITE_INFER_ACCEPT_THRESHOLD: '1.5' })).toThrow(
      /between 0 and 1/,
    );
    expect(() => loadConfig({ VITE_API_MODE: 'mock', VITE_INFER_MAX_FILES: '0' })).toThrow(
      ConfigError,
    );
  });

  it('reads the document caps, tiers and switches', () => {
    const defaults = loadConfig({ VITE_API_MODE: 'mock' });
    expect(defaults.documents).toEqual({
      maxFiles: 20,
      maxFileBytes: 20_971_520,
      fileSuffixes: ['.md', '.markdown', '.txt', '.pdf', '.docx'],
      defaultTiers: [0, 1],
      extractOnUpload: true,
      urlIntake: true,
      jobPollMs: 3000,
    });
    expect(defaults.extraction).toEqual({ acceptThreshold: 0.9, itemsPageSize: 200 });

    const config = loadConfig({
      VITE_API_MODE: 'mock',
      VITE_DOCUMENTS_MAX_FILES: '3',
      VITE_DOCUMENTS_MAX_FILE_BYTES: '4096',
      VITE_DOCUMENTS_FILE_SUFFIXES: '.MD, .pdf',
      VITE_DOCUMENTS_TIERS: '2, 0',
      VITE_DOCUMENTS_EXTRACT_ON_UPLOAD: 'false',
      VITE_DOCUMENTS_URL_INTAKE: 'false',
      VITE_DOCUMENTS_JOB_POLL_MS: '0',
      VITE_EXTRACTION_ACCEPT_THRESHOLD: '0.8',
      VITE_EXTRACTION_ITEMS_PAGE_SIZE: '25',
    });
    expect(config.documents).toEqual({
      maxFiles: 3,
      maxFileBytes: 4096,
      fileSuffixes: ['.md', '.pdf'],
      defaultTiers: [0, 2],
      extractOnUpload: false,
      urlIntake: false,
      jobPollMs: 0,
    });
    expect(config.extraction).toEqual({ acceptThreshold: 0.8, itemsPageSize: 25 });
    expect(() => loadConfig({ VITE_API_MODE: 'mock', VITE_DOCUMENTS_TIERS: '3' })).toThrow(
      /tiers among 0, 1 and 2/,
    );
    expect(() => loadConfig({ VITE_API_MODE: 'mock', VITE_DOCUMENTS_URL_INTAKE: 'yes' })).toThrow(
      /"true" or "false"/,
    );
  });
});

describe('withRuntimeConfig', () => {
  const build = { VITE_API_MODE: 'mock', VITE_KEEPER_BASE_URL: 'http://build.test', MODE: 'x' };

  it('maps the friendly aliases onto the VITE_ names', () => {
    const merged = withRuntimeConfig(build, {
      apiMode: 'live',
      keeperBaseUrl: 'https://keeper.example.test',
      apiToken: 'lbk_a',
    });
    expect(merged).toMatchObject({
      VITE_API_MODE: 'live',
      VITE_KEEPER_BASE_URL: 'https://keeper.example.test',
      VITE_API_TOKEN: 'lbk_a',
    });
    expect(withRuntimeConfig({}, { token: ' lbk_b ' })['VITE_API_TOKEN']).toBe('lbk_b');
  });

  it('passes VITE_ names through, stringifying plain numbers', () => {
    const merged = withRuntimeConfig(build, { VITE_PAGE_SIZE: 25, VITE_TOAST_MS: '7000' });
    expect(merged['VITE_PAGE_SIZE']).toBe('25');
    expect(merged['VITE_TOAST_MS']).toBe('7000');
  });

  it('lets the runtime win over the build and keeps the other build values', () => {
    const merged = withRuntimeConfig(build, { VITE_KEEPER_BASE_URL: 'http://runtime.test' });
    expect(merged['VITE_KEEPER_BASE_URL']).toBe('http://runtime.test');
    expect(merged['VITE_API_MODE']).toBe('mock');
    expect(merged['MODE']).toBe('x');
  });

  it('ignores empty, blank and invalid values and unknown keys', () => {
    const merged = withRuntimeConfig(build, {
      apiMode: '',
      keeperBaseUrl: '   ',
      VITE_PAGE_SIZE: null,
      VITE_TOAST_MS: Number.NaN,
      VITE_SEARCH_LIMIT: { a: 1 },
      VITE_CANVAS_DEPTH: ['2'],
      VITE_MOCK_LATENCY_MS: true,
      unknown: 'x',
      vite_lower: 'x',
      VITE_BAD$: 'x',
      __proto__: 'x',
      constructor: 'x',
    });
    expect(merged).toEqual(build);
  });

  it('ignores a runtime that is not a plain object', () => {
    for (const runtime of [undefined, null, 'live', 3, true, ['VITE_API_MODE']]) {
      expect(withRuntimeConfig(build, runtime)).toBe(build);
    }
  });

  it('switches a mock build to live mode from the runtime', () => {
    const config = loadConfig(
      withRuntimeConfig(build, { apiMode: 'live', keeperBaseUrl: 'https://keeper.example.test/' }),
    );
    expect(config.apiMode).toBe('live');
    expect(config.keeperBaseUrl).toBe('https://keeper.example.test');
    expect(loadConfig(withRuntimeConfig(build, {})).apiMode).toBe('mock');
  });

  it('still reports a missing mode when neither side sets one', () => {
    expect(() => loadConfig(withRuntimeConfig({}, { apiMode: '' }))).toThrow(/mock" or "live/);
  });
});
