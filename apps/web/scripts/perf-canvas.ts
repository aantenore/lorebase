/**
 * Measures the Canvas on a generated graph: time to the first drawn frame, main thread blocking,
 * and frame times while panning.
 *
 * Builds the app in mock mode with the generated graph of the mock API (`VITE_MOCK_PERF_NODES`,
 * `VITE_MOCK_PERF_EDGES`), serves it with `vite preview` and drives it in Chromium.
 *
 * `pnpm perf:canvas [--nodes 5000] [--edges 10000] [--runs 3]` (Node 24 runs this file
 * directly; needs `pnpm exec playwright install chromium`). Headless Chromium draws with a
 * software WebGL, so absolute numbers are lower bounds of a machine with a GPU; compare runs
 * made on the same machine.
 *
 * When the page marks its graph container with `data-layout` the script also waits for the
 * layout to stop and measures a second pan on the settled graph.
 */
import { createServer } from 'node:net';
import { resolve } from 'node:path';
import { chromium, type Page } from 'playwright';
import { build, preview } from 'vite';

const CONFIG = {
  route: '/supply/canvas',
  graph: '[aria-label^="Records graph"]',
  buildDir: 'node_modules/.cache/perf-canvas',
  browserArgs: [
    '--use-gl=angle',
    '--use-angle=swiftshader',
    '--enable-unsafe-swiftshader',
    '--ignore-gpu-blocklist',
  ],
  viewport: { width: 1440, height: 900 },
  /** Give up on the first draw / on the layout settling after this long. */
  firstRenderTimeoutMs: 120_000,
  settleTimeoutMs: 60_000,
  /** One pan: this many mouse moves of this many pixels, back and forth. */
  panMoves: 80,
  panStepPx: 6,
  /** Frames at or above this are counted as slow. */
  slowFrameMs: 50,
};

interface PanStats {
  frames: number;
  meanMs: number;
  p50Ms: number;
  p95Ms: number;
  maxMs: number;
  slowFrames: number;
  blockedMs: number;
}

interface RunResult {
  firstRenderMs: number;
  /** Time on the main thread in long tasks (>50 ms, counted past 50) before the first draw. */
  blockedBeforeFirstMs: number;
  longestTaskMs: number;
  panDuringLayout: PanStats;
  layoutState: string | null;
  settledAfterMs: number | null;
  panSettled: PanStats | null;
}

function option(name: string, fallback: number): number {
  const args = process.argv.slice(2);
  const index = args.findIndex((arg) => arg === `--${name}`);
  const value = Number(args[index + 1]);
  return index >= 0 && Number.isFinite(value) ? value : fallback;
}

function freePort(): Promise<number> {
  return new Promise((done, fail) => {
    const probe = createServer();
    probe.once('error', fail);
    probe.listen(0, '127.0.0.1', () => {
      const address = probe.address();
      const port = typeof address === 'object' && address !== null ? address.port : 0;
      probe.close(() => {
        done(port);
      });
    });
  });
}

/** Installed before the page loads: records the first drawn frame and long tasks. */
function installProbes(graphSelector: string): void {
  interface Probe {
    first: number | null;
    tasks: { start: number; duration: number }[];
    frames: number[] | null;
  }
  const probe: Probe = { first: null, tasks: [], frames: null };
  (window as unknown as { __probe: Probe }).__probe = probe;
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      probe.tasks.push({ start: entry.startTime, duration: entry.duration });
    }
  }).observe({ type: 'longtask', buffered: true });
  let waiting = false;
  new MutationObserver(() => {
    if (waiting || probe.first !== null) return;
    if (document.querySelector(`${graphSelector} canvas`) === null) return;
    waiting = true;
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        probe.first = performance.now();
      });
    });
  }).observe(document, { childList: true, subtree: true });
}

function percentile(sorted: number[], p: number): number {
  return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * p))] ?? 0;
}

async function pan(page: Page): Promise<PanStats> {
  const { panMoves, panStepPx, slowFrameMs } = CONFIG;
  const box = await page.locator(CONFIG.graph).boundingBox();
  if (box === null) throw new Error('the graph container is not visible');
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;
  await page.evaluate(() => {
    const probe = (
      window as unknown as { __probe: { frames: number[] | null; tasks: unknown[]; stop: boolean } }
    ).__probe;
    probe.frames = [];
    probe.stop = false;
    let last = performance.now();
    const tick = (now: number) => {
      probe.frames?.push(now - last);
      last = now;
      if (!probe.stop) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
  const tasksBefore = await page.evaluate(
    () => (window as unknown as { __probe: { tasks: unknown[] } }).__probe.tasks.length,
  );
  await page.mouse.move(cx, cy);
  await page.mouse.down();
  for (let i = 1; i <= panMoves; i += 1) {
    const phase = Math.floor((i * 4) / panMoves) % 2 === 0 ? 1 : -1;
    await page.mouse.move(cx + phase * panStepPx * (i % (panMoves / 4)), cy);
  }
  await page.mouse.up();
  const { frames, blocked } = await page.evaluate((from) => {
    const probe = (
      window as unknown as {
        __probe: { frames: number[]; stop: boolean; tasks: { duration: number }[] };
      }
    ).__probe;
    probe.stop = true;
    return {
      frames: probe.frames,
      blocked: probe.tasks.slice(from).reduce((sum, t) => sum + Math.max(0, t.duration - 50), 0),
    };
  }, tasksBefore);
  const sorted = [...frames].sort((a, b) => a - b);
  const mean = frames.reduce((a, b) => a + b, 0) / Math.max(frames.length, 1);
  return {
    frames: frames.length,
    meanMs: mean,
    p50Ms: percentile(sorted, 0.5),
    p95Ms: percentile(sorted, 0.95),
    maxMs: sorted[sorted.length - 1] ?? 0,
    slowFrames: frames.filter((f) => f >= slowFrameMs).length,
    blockedMs: blocked,
  };
}

async function measure(origin: string): Promise<RunResult> {
  const browser = await chromium.launch({ args: CONFIG.browserArgs });
  try {
    const context = await browser.newContext({ viewport: CONFIG.viewport });
    const page = await context.newPage();
    await page.addInitScript(installProbes, CONFIG.graph);
    await page.goto(`${origin}${CONFIG.route}`);
    await page.waitForFunction(
      () => (window as unknown as { __probe: { first: number | null } }).__probe.first !== null,
      undefined,
      { timeout: CONFIG.firstRenderTimeoutMs, polling: 50 },
    );
    const first = await page.evaluate(() => {
      const probe = (
        window as unknown as {
          __probe: { first: number; tasks: { start: number; duration: number }[] };
        }
      ).__probe;
      const before = probe.tasks.filter((t) => t.start < probe.first);
      return {
        at: probe.first,
        blocked: before.reduce((sum, t) => sum + Math.max(0, t.duration - 50), 0),
        longest: before.reduce((max, t) => Math.max(max, t.duration), 0),
      };
    });
    const panDuringLayout = await pan(page);
    // The attribute appears once the worker layout exists; a build without one never sets it.
    await page
      .waitForFunction(
        (selector) => document.querySelector(selector)?.hasAttribute('data-layout') === true,
        CONFIG.graph,
        { timeout: 3000 },
      )
      .catch(() => undefined);
    const layoutState = await page
      .locator(CONFIG.graph)
      .getAttribute('data-layout')
      .catch(() => null);
    let settledAfterMs: number | null = null;
    let panSettled: PanStats | null = null;
    if (layoutState !== null) {
      await page.waitForFunction(
        (selector) => document.querySelector(selector)?.getAttribute('data-layout') !== 'running',
        CONFIG.graph,
        { timeout: CONFIG.settleTimeoutMs, polling: 100 },
      );
      settledAfterMs = (await page.evaluate(() => performance.now())) - first.at;
      panSettled = await pan(page);
    }
    return {
      firstRenderMs: first.at,
      blockedBeforeFirstMs: first.blocked,
      longestTaskMs: first.longest,
      panDuringLayout,
      layoutState: await page
        .locator(CONFIG.graph)
        .getAttribute('data-layout')
        .catch(() => null),
      settledAfterMs,
      panSettled,
    };
  } finally {
    await browser.close();
  }
}

const fmt = (n: number) => n.toFixed(1);
const median = (values: number[]) =>
  [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)] ?? 0;

function describePan(label: string, runs: PanStats[]): string {
  return (
    `${label}: frame mean ${fmt(median(runs.map((r) => r.meanMs)))} ms, ` +
    `p95 ${fmt(median(runs.map((r) => r.p95Ms)))} ms, max ${fmt(median(runs.map((r) => r.maxMs)))} ms, ` +
    `slow frames ${median(runs.map((r) => r.slowFrames)).toString()} of ${median(runs.map((r) => r.frames)).toString()}, ` +
    `blocked ${fmt(median(runs.map((r) => r.blockedMs)))} ms`
  );
}

async function main(): Promise<void> {
  const nodes = option('nodes', 5000);
  const edges = option('edges', 10_000);
  const runs = option('runs', 3);
  const root = resolve(import.meta.dirname, '..');
  process.env['VITE_API_MODE'] = 'mock';
  process.env['VITE_MOCK_PERF_NODES'] = String(nodes);
  process.env['VITE_MOCK_PERF_EDGES'] = String(edges);
  process.env['VITE_CANVAS_MAX_NODES'] = String(nodes);
  await build({ root, logLevel: 'warn', build: { outDir: CONFIG.buildDir, emptyOutDir: true } });
  const port = await freePort();
  const server = await preview({
    root,
    logLevel: 'warn',
    build: { outDir: CONFIG.buildDir },
    preview: { host: '127.0.0.1', port, strictPort: true },
  });
  try {
    const results: RunResult[] = [];
    for (let i = 0; i < runs; i += 1) results.push(await measure(`http://127.0.0.1:${port}`));
    console.log(
      `${nodes.toString()} nodes, ${edges.toString()} edges, ${runs.toString()} runs (median)`,
    );
    console.log(`first render: ${fmt(median(results.map((r) => r.firstRenderMs)))} ms`);
    console.log(
      `main thread blocked before it: ${fmt(median(results.map((r) => r.blockedBeforeFirstMs)))} ms ` +
        `(longest task ${fmt(median(results.map((r) => r.longestTaskMs)))} ms)`,
    );
    console.log(
      describePan(
        'pan right after first render',
        results.map((r) => r.panDuringLayout),
      ),
    );
    const settled = results.flatMap((r) => (r.panSettled === null ? [] : [r.panSettled]));
    if (settled.length > 0) {
      const after = results.flatMap((r) => (r.settledAfterMs === null ? [] : [r.settledAfterMs]));
      console.log(`layout stopped ${fmt(median(after))} ms after first render`);
      console.log(describePan('pan on the settled graph', settled));
    }
    console.log(JSON.stringify(results));
  } finally {
    await server.close();
  }
}

main().then(
  () => {
    process.exit(0);
  },
  (error: unknown) => {
    console.error(error);
    process.exit(1);
  },
);
