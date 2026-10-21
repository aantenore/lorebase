import { defineConfig, devices } from '@playwright/test';
import { e2eEnv } from './e2e/env';

/**
 * End-to-end tests of the web app against a real Keeper (`pnpm e2e`, see README, "End-to-end
 * tests"). The app is built in live mode and served by `vite preview` with a same-origin proxy
 * to Keeper, so the browser needs no CORS setup. The global setup brings up Keeper (or uses
 * LOREBASE_E2E_URL) and loads the supply Domain.
 */
const env = e2eEnv();
const cache = 'node_modules/.cache/e2e';
const ci = process.env['CI'] !== undefined;

export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.spec.ts',
  globalSetup: './e2e/global-setup.ts',
  // The flows build on each other's Proposals and merge into the same Branch.
  fullyParallel: false,
  workers: 1,
  forbidOnly: ci,
  retries: ci ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  outputDir: `${cache}/results`,
  reporter: ci
    ? [['list'], ['html', { open: 'never', outputFolder: `${cache}/report` }]]
    : [['list']],
  use: {
    baseURL: env.webUrl,
    actionTimeout: 10_000,
    navigationTimeout: 20_000,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    // The Canvas needs WebGL 2; headless Chromium gets a software one.
    launchOptions: {
      args: [
        '--use-gl=angle',
        '--use-angle=swiftshader',
        '--enable-unsafe-swiftshader',
        '--ignore-gpu-blocklist',
      ],
    },
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
    },
  ],
  webServer: {
    command: [
      `pnpm exec vite build --outDir ${cache}/dist --emptyOutDir`,
      `pnpm exec vite preview --outDir ${cache}/dist --host 127.0.0.1 --port ${env.webPort} --strictPort`,
    ].join(' && '),
    url: env.webUrl,
    reuseExistingServer: false,
    timeout: 180_000,
    env: {
      VITE_API_MODE: 'live',
      VITE_KEEPER_BASE_URL: '/',
      LOREBASE_KEEPER_PROXY: env.keeperUrl,
    },
  },
});
