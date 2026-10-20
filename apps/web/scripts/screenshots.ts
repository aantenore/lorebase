/**
 * Captures the screenshots used by docs/tour.md and the README.
 *
 * Builds the app in mock mode into a cache folder, serves it with `vite preview` on a free
 * port, visits every shot of SHOTS in Chromium and writes docs/images/<name>.png.
 *
 * Run with `pnpm screenshots` (Node 24 runs this file directly). Needs the Chromium build of
 * Playwright: `pnpm exec playwright install chromium`.
 *
 * `pnpm screenshots flow` records a short walk through the app instead (Explorer, Canvas, the
 * Review resolver) and writes docs/images/tour.gif. It encodes the video with ffmpeg when it is
 * installed, otherwise it stitches PNG frames with Pillow (`uv run --with pillow`).
 */
import { spawnSync } from 'node:child_process';
import { mkdir, mkdtemp, readdir, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { createServer } from 'node:net';
import { relative, resolve } from 'node:path';
import { chromium, type Browser, type Page } from 'playwright';
import { build, preview, type PreviewServer } from 'vite';

type Theme = 'light' | 'dark';

/** A small scripted action run after the page is ready and before the picture is taken. */
type Step =
  | { click: string }
  | { scrollToTop: string; offset?: number }
  | { wait: number }
  | { waitFor: string }
  | { check: string; nth: number };

interface Shot {
  /** File name without extension: docs/images/<name>.png. */
  name: string;
  /** Route to visit, including the search string. */
  path: string;
  /** Selector that must be visible before the picture is taken. */
  ready: string;
  theme?: Theme;
  steps?: Step[];
  /** The picture is only valid when the Sigma canvas drew; otherwise the list fallback is reported. */
  needsWebGL?: boolean;
}

const DOMAIN = 'supply';

const CONFIG = {
  viewport: { width: 1440, height: 900 },
  defaultTheme: 'light' as Theme,
  themeStorageKey: 'lorebase.theme',
  /** Mock data only: the build never reads a .env file for this value. */
  apiMode: 'mock',
  outputDir: resolve(import.meta.dirname, '../../../docs/images'),
  buildDir: resolve(import.meta.dirname, '../node_modules/.cache/screenshots-dist'),
  readyTimeoutMs: 15_000,
  /** Pause after the page is ready, so layout (ForceAtlas2) and fonts have settled. */
  settleMs: 600,
  /** Chromium flags that give headless mode a software WebGL 2 (Sigma needs it). */
  browserArgs: [
    '--use-gl=angle',
    '--use-angle=swiftshader',
    '--enable-unsafe-swiftshader',
    '--ignore-gpu-blocklist',
  ],
  /** Soft limit for one PNG, reported as a warning. */
  maxImageBytes: 400 * 1024,
};

const SHOTS: Shot[] = [
  {
    name: 'explorer-home',
    path: `/${DOMAIN}/explorer`,
    ready: 'text=Recent Revisions',
  },
  {
    name: 'explorer-kind',
    path: `/${DOMAIN}/explorer/Supplier`,
    ready: 'text=S-1001',
  },
  {
    name: 'explorer-record',
    path: `/${DOMAIN}/explorer/Supplier/S-1001`,
    ready: 'text=Brevik Bearings',
  },
  {
    name: 'canvas-graph',
    path: `/${DOMAIN}/canvas`,
    ready: '[role=img][aria-label^="Records graph"]',
    needsWebGL: true,
  },
  {
    name: 'canvas-schema',
    path: `/${DOMAIN}/canvas?view=schema`,
    ready: '[role=img][aria-label^="Schema graph"]',
    needsWebGL: true,
  },
  {
    name: 'modeler-list',
    path: `/${DOMAIN}/modeler`,
    ready: 'text=Supplier',
  },
  {
    name: 'modeler-edit',
    path: `/${DOMAIN}/modeler?view=edit`,
    ready: '.cm-content',
  },
  {
    name: 'review-list',
    path: `/${DOMAIN}/review`,
    ready: 'text=Risk model',
  },
  {
    name: 'review-conflict',
    path: `/${DOMAIN}/review/41`,
    ready: '[role=listbox][aria-label="Conflicts"] [role=option]',
    steps: [
      { click: '[role=listbox][aria-label="Conflicts"] [role=option]' },
      { wait: 400 },
      { scrollToTop: 'text=Merge attempt', offset: 80 },
    ],
  },
  {
    name: 'settings',
    path: `/${DOMAIN}/settings`,
    ready: 'text=Appearance',
  },
  {
    name: 'explorer-home-dark',
    path: `/${DOMAIN}/explorer`,
    ready: 'text=Recent Revisions',
    theme: 'dark',
  },
];

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

async function startServer(): Promise<{ server: PreviewServer; origin: string }> {
  const root = resolve(import.meta.dirname, '..');
  // Vite gives process variables precedence over .env files.
  process.env['VITE_API_MODE'] = CONFIG.apiMode;
  await build({
    root,
    logLevel: 'warn',
    build: { outDir: CONFIG.buildDir, emptyOutDir: true },
  });
  const port = await freePort();
  const server = await preview({
    root,
    logLevel: 'warn',
    build: { outDir: CONFIG.buildDir },
    preview: { host: '127.0.0.1', port, strictPort: true },
  });
  return { server, origin: `http://127.0.0.1:${port.toString()}` };
}

async function runStep(page: Page, step: Step): Promise<void> {
  if ('click' in step) {
    await page.locator(step.click).first().click();
  } else if ('scrollToTop' in step) {
    await page
      .locator(step.scrollToTop)
      .first()
      .evaluate((element, offset) => {
        element.scrollIntoView({ block: 'start' });
        window.scrollBy(0, -offset);
      }, step.offset ?? 0);
  } else if ('waitFor' in step) {
    await page
      .locator(step.waitFor)
      .first()
      .waitFor({ state: 'visible', timeout: CONFIG.readyTimeoutMs });
  } else if ('check' in step) {
    await page.locator(step.check).nth(step.nth).check();
  } else {
    await page.waitForTimeout(step.wait);
  }
}

/** True when a Sigma canvas exists inside the graph container and the fallback message is absent. */
async function canvasDrew(page: Page, ready: string): Promise<boolean> {
  const container = page.locator(ready).first();
  const fallback = await container.locator('[role=status]').count();
  const canvases = await container.locator('canvas').count();
  return fallback === 0 && canvases > 0;
}

async function capture(browser: Browser, origin: string, shot: Shot): Promise<string> {
  const theme = shot.theme ?? CONFIG.defaultTheme;
  const context = await browser.newContext({
    viewport: CONFIG.viewport,
    colorScheme: theme,
    reducedMotion: 'reduce',
  });
  try {
    await context.addInitScript(
      ([key, value]) => {
        window.localStorage.setItem(key, value);
      },
      [CONFIG.themeStorageKey, theme] as const,
    );
    const page = await context.newPage();
    const failures: string[] = [];
    page.on('pageerror', (error) => {
      failures.push(error.message);
    });
    await page.goto(`${origin}${shot.path}`);
    await page
      .locator(shot.ready)
      .first()
      .waitFor({ state: 'visible', timeout: CONFIG.readyTimeoutMs });
    await page.evaluate(() => document.fonts.ready);
    for (const step of shot.steps ?? []) await runStep(page, step);
    await page.waitForTimeout(CONFIG.settleMs);

    let note = '';
    if (shot.needsWebGL === true && !(await canvasDrew(page, shot.ready))) {
      note = ' [WebGL canvas did not render: list fallback captured]';
    }
    if (failures.length > 0) note += ` [page errors: ${failures.join('; ')}]`;

    const file = resolve(CONFIG.outputDir, `${shot.name}.png`);
    await page.screenshot({ path: file, animations: 'disabled' });
    const { size } = await stat(file);
    if (size > CONFIG.maxImageBytes) note += ' [over the size limit]';
    return `${relative(process.cwd(), file)} ${Math.round(size / 1024).toString()} KB${note}`;
  } finally {
    await context.close();
  }
}

/**
 * The scripted walk behind docs/images/tour.gif: Explorer, a Record, the Canvas and the Review
 * resolver on Proposal 41. Every step reads like a user action; waits let the picture settle.
 */
const FLOW = {
  start: `/${DOMAIN}/explorer`,
  ready: 'text=Recent Revisions',
  viewport: { width: 1280, height: 800 },
  output: resolve(import.meta.dirname, '../../../docs/images/tour.gif'),
  /** GIF width in pixels, frame rate and palette size: these decide the file size. */
  gif: { width: 880, fps: 8, colors: 96 },
  maxBytes: 2 * 1024 * 1024,
  steps: [
    { wait: 1400 },
    { click: '[aria-label="Main"] >> text=Explorer' },
    { click: 'a:has-text("Supplier")' },
    { waitFor: 'text=S-1001' },
    { wait: 1000 },
    { click: 'a:has-text("Brevik Bearings")' },
    { waitFor: 'text=Brevik Bearings' },
    { wait: 1600 },
    { click: 'a:has-text("Open in Canvas")' },
    { waitFor: '[role=img][aria-label^="Records graph"]' },
    { wait: 2600 },
    { click: '[aria-label="Main"] >> text=Review' },
    { waitFor: 'text=Risk model' },
    { wait: 1200 },
    { click: 'a:has-text("Risk model")' },
    { waitFor: '[role=listbox][aria-label="Conflicts"] [role=option]' },
    { click: '[role=listbox][aria-label="Conflicts"] [role=option]' },
    { wait: 500 },
    { scrollToTop: 'text=Merge attempt', offset: 80 },
    { wait: 1800 },
    { check: 'fieldset input[type=radio]', nth: 1 },
    { wait: 900 },
    { click: 'button:has-text("Save resolution")' },
    { wait: 2200 },
  ] as Step[],
};

/** Encodes the recorded WebM as a GIF with a two-pass palette, which keeps the file small. */
function encodeWithFfmpeg(video: string): boolean {
  if (spawnSync('ffmpeg', ['-version'], { stdio: 'ignore' }).status !== 0) return false;
  const { width, fps, colors } = FLOW.gif;
  const filters =
    `fps=${fps.toString()},scale=${width.toString()}:-1:flags=lanczos,split[a][b];` +
    `[a]palettegen=max_colors=${colors.toString()}:stats_mode=diff[p];` +
    `[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle`;
  const run = spawnSync(
    'ffmpeg',
    [
      '-y',
      '-loglevel',
      'error',
      '-i',
      video,
      '-filter_complex',
      filters,
      '-loop',
      '0',
      FLOW.output,
    ],
    { stdio: 'inherit' },
  );
  return run.status === 0;
}

const PILLOW_SCRIPT = `
import sys
from pathlib import Path
from PIL import Image

frames, out, width, ms = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
images = []
for path in sorted(frames.glob("frame-*.png")):
    image = Image.open(path).convert("RGB")
    height = round(image.height * width / image.width)
    images.append(image.resize((width, height), Image.LANCZOS).quantize(colors=96))
images[0].save(out, save_all=True, append_images=images[1:], duration=ms, loop=0, optimize=True)
`;

/** Fallback when ffmpeg is missing: PNG frames taken at the key steps, stitched with Pillow. */
function encodeWithPillow(frames: string): boolean {
  const run = spawnSync(
    'uv',
    [
      'run',
      '--with',
      'pillow',
      'python',
      '-c',
      PILLOW_SCRIPT,
      frames,
      FLOW.output,
      String(FLOW.gif.width),
      '1500',
    ],
    { stdio: 'inherit' },
  );
  return run.status === 0;
}

async function recordFlow(browser: Browser, origin: string): Promise<void> {
  const work = await mkdtemp(resolve(tmpdir(), 'lorebase-flow-'));
  const frames = resolve(work, 'frames');
  await mkdir(frames);
  const context = await browser.newContext({
    viewport: FLOW.viewport,
    colorScheme: CONFIG.defaultTheme,
    recordVideo: { dir: work, size: FLOW.viewport },
  });
  try {
    await context.addInitScript(
      ([key, value]) => {
        window.localStorage.setItem(key, value);
      },
      [CONFIG.themeStorageKey, CONFIG.defaultTheme] as const,
    );
    const page = await context.newPage();
    await page.goto(`${origin}${FLOW.start}`);
    await page
      .locator(FLOW.ready)
      .first()
      .waitFor({ state: 'visible', timeout: CONFIG.readyTimeoutMs });
    await page.evaluate(() => document.fonts.ready);
    let index = 0;
    for (const step of FLOW.steps) {
      await runStep(page, step);
      if ('wait' in step && step.wait >= 1000) {
        index += 1;
        await page.screenshot({
          path: resolve(frames, `frame-${index.toString().padStart(2, '0')}.png`),
        });
      }
    }
  } finally {
    await context.close();
  }
  const videos = (await readdir(work)).filter((name) => name.endsWith('.webm'));
  const video = videos[0];
  const useFfmpeg = process.env['FLOW_ENCODER'] !== 'pillow';
  const encoded =
    (useFfmpeg && video !== undefined && encodeWithFfmpeg(resolve(work, video))) ||
    encodeWithPillow(frames);
  await rm(work, { recursive: true, force: true });
  if (!encoded)
    throw new Error('could not encode the GIF (need ffmpeg, or uv with network for Pillow)');
  const { size } = await stat(FLOW.output);
  const note = size > FLOW.maxBytes ? ' [over the 2 MB limit: do not commit]' : '';
  console.log(
    `${relative(process.cwd(), FLOW.output)} ${Math.round(size / 1024).toString()} KB${note}`,
  );
}

async function main(): Promise<void> {
  await mkdir(CONFIG.outputDir, { recursive: true });
  const { server, origin } = await startServer();
  const browser = await chromium.launch({ args: CONFIG.browserArgs });
  try {
    if (process.argv[2] === 'flow') {
      await recordFlow(browser, origin);
    } else {
      for (const shot of SHOTS) {
        console.log(`${shot.name}: ${await capture(browser, origin, shot)}`);
      }
    }
  } finally {
    await browser.close();
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
