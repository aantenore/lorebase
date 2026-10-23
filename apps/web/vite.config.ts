import { fileURLToPath, URL } from 'node:url';
import tailwindcss from '@tailwindcss/vite';
import { tanstackRouter } from '@tanstack/router-plugin/vite';
import react from '@vitejs/plugin-react';
import { visualizer } from 'rollup-plugin-visualizer';
import { defineConfig } from 'vitest/config';

const isTest = process.env['VITEST'] !== undefined;

// Optional same-origin proxy to a Keeper, for `vite` and `vite preview`: the browser then
// talks to the app's own origin (VITE_KEEPER_BASE_URL=/), so no CORS setup is needed and
// Keeper's ETag header is readable. Server-side only; never part of the bundle.
// LOREBASE_GUIDE_PROXY does the same for a guide service under /guide (VITE_GUIDE_URL=/guide).
const keeperProxy = process.env['LOREBASE_KEEPER_PROXY'];
const guideProxy = process.env['LOREBASE_GUIDE_PROXY'];
const proxyEntries = {
  ...(keeperProxy === undefined ? {} : { '/api': { target: keeperProxy } }),
  // Guide checks the Origin against its allowed origins: present the proxied calls as its own.
  ...(guideProxy === undefined
    ? {}
    : {
        '/guide': {
          target: guideProxy,
          changeOrigin: true,
          headers: { origin: new URL(guideProxy).origin },
        },
      }),
};
const proxy = Object.keys(proxyEntries).length === 0 ? undefined : proxyEntries;

// Vendor groups, one chunk each. A group only loads where its code is imported, so the
// editor and graph libraries stay out of the first load: they are reached through dynamic
// imports. Group order is priority order.
const VENDOR_GROUPS = [
  { name: 'react', packages: ['react', 'react-dom', 'scheduler'] },
  { name: 'tanstack', packages: ['@tanstack'] },
  {
    name: 'graph',
    packages: ['sigma', 'graphology', 'graphology-layout-forceatlas2', 'graphology-utils'],
  },
  { name: 'codemirror', packages: ['@codemirror', '@lezer', 'crelt', 'style-mod', 'w3c-keyname'] },
  { name: 'yaml', packages: ['yaml'] },
].map(({ name, packages }) => ({
  name,
  test: new RegExp(`node_modules[\\\\/](${packages.join('|')})[\\\\/]`),
}));

// `pnpm analyze` (mode "analyze") also writes a treemap of the bundle to a gitignored folder.
const ANALYZE_DIR = 'node_modules/.cache/analyze';

export default defineConfig(({ mode }) => ({
  plugins: [
    // Routes load their screens with lazyRouteComponent, so the plugin's own splitting is off.
    // The route tree is committed; regenerate it only for dev and build.
    ...(isTest ? [] : [tanstackRouter({ target: 'react', autoCodeSplitting: false })]),
    react(),
    tailwindcss(),
    ...(mode === 'analyze'
      ? [
          visualizer({
            filename: `${ANALYZE_DIR}/stats.html`,
            template: 'treemap',
            gzipSize: true,
            brotliSize: true,
          }),
        ]
      : []),
  ],
  build: {
    rolldownOptions: {
      output: { codeSplitting: { groups: VENDOR_GROUPS } },
    },
    ...(mode === 'analyze' ? { outDir: `${ANALYZE_DIR}/dist`, emptyOutDir: true } : {}),
  },
  server: { ...(proxy === undefined ? {} : { proxy }) },
  preview: { ...(proxy === undefined ? {} : { proxy }) },
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}', 'scripts/**/*.test.ts'],
    css: false,
  },
}));
