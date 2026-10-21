import { fileURLToPath, URL } from 'node:url';
import tailwindcss from '@tailwindcss/vite';
import { tanstackRouter } from '@tanstack/router-plugin/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

const isTest = process.env['VITEST'] !== undefined;

// Optional same-origin proxy to a Keeper, for `vite` and `vite preview`: the browser then
// talks to the app's own origin (VITE_KEEPER_BASE_URL=/), so no CORS setup is needed and
// Keeper's ETag header is readable. Server-side only; never part of the bundle.
const keeperProxy = process.env['LOREBASE_KEEPER_PROXY'];
const proxy = keeperProxy === undefined ? undefined : { '/api': { target: keeperProxy } };

export default defineConfig({
  plugins: [
    // The route tree is committed; regenerate it only for dev and build.
    ...(isTest ? [] : [tanstackRouter({ target: 'react', autoCodeSplitting: true })]),
    react(),
    tailwindcss(),
  ],
  server: { ...(proxy === undefined ? {} : { proxy }) },
  preview: { ...(proxy === undefined ? {} : { proxy }) },
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    css: false,
  },
});
