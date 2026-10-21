import { ConfigError, loadConfig } from './env';

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
});
