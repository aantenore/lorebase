/**
 * Captures the screenshots used by docs/tour.md and the README.
 *
 * Builds the app in mock mode into a cache folder, serves it with `vite preview` on a free
 * port, visits every shot of SHOTS in Chromium and writes docs/images/<name>.png.
 *
 * Run with `pnpm screenshots` (Node 24 runs this file directly). Needs the Chromium build of
 * Playwright: `pnpm exec playwright install chromium`.
 *
 * `pnpm screenshots --live --keeper http://127.0.0.1:8080` takes the same pictures from the app
 * in live mode against a running Keeper (the option falls back to LOREBASE_E2E_URL). It loads
 * the supply sample under a Domain of its own through the REST API (the loader of the
 * end-to-end tests), prepares a Proposal with Conflicts, and drops the Domain afterwards unless
 * `--keep` is given. The app is served with a same-origin proxy to Keeper, so Keeper needs no
 * CORS settings. Keeper must accept the setup's own review and merge of one Proposal, so run it
 * with `keeper.proposals.allow_self_approval: true` (authentication off, as in development).
 *
 * `pnpm screenshots --live --keeper http://127.0.0.1:8090 --domain supply --guide http://127.0.0.1:8090`
 * takes the pictures from a running demo stack instead, read only: it uses the existing Domain
 * as it is (nothing is loaded, reviewed or dropped), serves the current build of the app with a
 * same-origin proxy to that Keeper (`/api`) and guide service (`/guide`), and asks the guide one
 * question for the Explore picture. The pictures a demo stack cannot show (Start from data,
 * Source runs, a Proposal with Conflicts) come from the mock: `pnpm screenshots --only
 * modeler-start,modeler-source-mapping,sources,review-conflict`. docs/tour.md lists which is which.
 *
 * `--only documents,review-items` takes just the named pictures.
 *
 * `pnpm screenshots flow` records a short walk through the app instead (Explorer, Canvas, the
 * Modeler Source mapping and its preview, the Review resolver) and writes docs/images/tour.gif. It encodes the video with ffmpeg when it is
 * installed, otherwise it stitches PNG frames with Pillow (`uv run --with pillow`).
 */
import { spawnSync } from 'node:child_process';
import { mkdir, mkdtemp, readdir, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { createServer } from 'node:net';
import { relative, resolve } from 'node:path';
import { chromium, type Browser, type Page } from 'playwright';
import { build, preview, type PreviewServer } from 'vite';
import { createKeeper, waitForKeeper } from '../e2e/keeper.ts';
import { dropDomain, loadSupply, prepareConflicts } from '../e2e/supply.ts';

type Theme = 'light' | 'dark';

/** A small scripted action run after the page is ready and before the picture is taken. */
type Step =
  | { click: string }
  | { scrollToTop: string; offset?: number }
  | { wait: number }
  | { waitFor: string; timeoutMs?: number }
  | { fill: string; text: string }
  | { check: string; nth: number }
  | { upload: string; files: string[]; from?: 'sourceSample' };

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
  /** The graph container checked by `needsWebGL`, when it is not `ready`. */
  graph?: string;
  /** Viewport height for a long screen; the width is always the one of CONFIG. */
  height?: number;
}

/** The sample Domain of the mock; a live run uses a Domain it loads itself. */
const MOCK_DOMAIN = 'supply';

const CONFIG = {
  viewport: { width: 1440, height: 900 },
  defaultTheme: 'light' as Theme,
  themeStorageKey: 'lorebase.theme',
  /** Mock data only: the build never reads a .env file for this value. */
  apiMode: 'mock',
  outputDir: resolve(import.meta.dirname, '../../../docs/images'),
  /** CSV files the "Start from data" shot uploads: the demo Domain of examples/supply. */
  sampleDataDir: resolve(import.meta.dirname, '../../../examples/supply/data'),
  /** Sample file the Source preview shot reads: columns of the mock Source erp.vendors. */
  sourceSampleDir: resolve(import.meta.dirname, 'screenshot-data'),
  buildDir: resolve(import.meta.dirname, '../node_modules/.cache/screenshots-dist'),
  /** Live builds bake in other variables, so they get their own folder. */
  liveBuildDir: resolve(import.meta.dirname, '../node_modules/.cache/screenshots-dist-live'),
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

const MOCK_SHOTS = (domain: string): Shot[] => [
  {
    name: 'explorer-home',
    path: `/${domain}/explorer`,
    ready: 'text=Recent Revisions',
  },
  {
    name: 'explorer-kind',
    path: `/${domain}/explorer/Supplier`,
    ready: 'text=S-1001',
  },
  {
    name: 'explorer-record',
    path: `/${domain}/explorer/Supplier/S-1001`,
    ready: 'text=Brevik Bearings',
  },
  {
    name: 'canvas-graph',
    path: `/${domain}/canvas`,
    ready: '[role=img][aria-label^="Records graph"]',
    needsWebGL: true,
  },
  {
    name: 'canvas-schema',
    path: `/${domain}/canvas?view=schema`,
    ready: '[role=img][aria-label^="Schema graph"]',
    needsWebGL: true,
  },
  {
    name: 'agent-activity',
    path: `/${domain}/agent-activity?session=sess_20261020_brevik&step=4`,
    ready: '[role=img][aria-label^="Records touched"]',
    needsWebGL: true,
  },
  {
    name: 'retrieval-lab',
    path: `/${domain}/retrieval-lab?q=brevik%20beerings&budget=1200`,
    ready: '[role=img][aria-label^="Involved Records"]',
    needsWebGL: true,
  },
  {
    // The scripted exploration of the mock: an example question, its steps and the cited Records.
    name: 'explore',
    path: `/${domain}/explore`,
    ready: '[aria-label="Example questions"]',
    steps: [
      { click: '[aria-label="Example questions"] >> text=Single-source risk' },
      { waitFor: '[aria-label="Answer"] >> text=View trace' },
      { click: 'text=Show cited Records on the Canvas' },
      { waitFor: '[role=img][aria-label^="Cited Records"]' },
    ],
  },
  {
    name: 'modeler-list',
    path: `/${domain}/modeler`,
    ready: 'text=Supplier',
  },
  {
    name: 'modeler-edit',
    path: `/${domain}/modeler?view=edit`,
    ready: '.cm-content',
  },
  {
    name: 'modeler-start',
    path: `/${domain}/modeler?view=start`,
    ready: 'button:has-text("Run inference")',
    steps: [
      {
        upload: 'input[type=file]',
        files: [
          'contract',
          'delivered_to',
          'flags',
          'for_part',
          'part',
          'plant',
          'purchase_order',
          'risk_flag',
          'stocked_at',
          'supplied_by',
          'supplier',
          'with_supplier',
        ].map((table) => `${table}.csv`),
      },
      { click: 'button:has-text("Run inference")' },
      { waitFor: 'text=Proposed schema' },
      { click: 'button:has-text("Accept all at or above")' },
      { wait: 400 },
      { scrollToTop: 'h1', offset: 80 },
    ],
  },
  {
    name: 'modeler-source-mapping',
    path: `/${domain}/modeler?view=edit`,
    ready: '.cm-content',
    steps: [
      { click: '[role=tab]:has-text("Sources")' },
      { waitFor: 'text=Transforms of legal_name' },
      { scrollToTop: 'text=Attribute mapping', offset: 120 },
      { wait: 300 },
    ],
  },
  {
    name: 'modeler-source-preview',
    path: `/${domain}/modeler?view=edit`,
    ready: '.cm-content',
    steps: [
      { click: '[role=tab]:has-text("Sources")' },
      { upload: 'input[type=file]', files: ['vendors.csv'], from: 'sourceSample' },
      { waitFor: 'table[aria-label^="Sample rows"]' },
      { scrollToTop: 'table[aria-label^="Sample rows"]', offset: 260 },
      { wait: 300 },
    ],
  },
  {
    name: 'modeler-operation-steps',
    path: `/${domain}/modeler?view=edit&branch=model%2Frisk`,
    ready: '.cm-content',
    steps: [
      { click: '[role=tab]:has-text("Operations")' },
      { waitFor: 'text=Effects, in order.' },
      { scrollToTop: 'text=Effects, in order.', offset: 120 },
      { wait: 300 },
    ],
  },
  {
    name: 'sources',
    path: `/${domain}/sources`,
    ready: 'text=run_7',
    steps: [{ waitFor: 'text=Run history' }],
  },
  {
    name: 'review-list',
    path: `/${domain}/review`,
    ready: 'text=Risk model',
  },
  {
    name: 'review-conflict',
    path: `/${domain}/review/41`,
    ready: '[role=listbox][aria-label="Conflicts"] [role=option]',
    steps: [
      { click: '[role=listbox][aria-label="Conflicts"] [role=option]' },
      { wait: 400 },
      { scrollToTop: 'text=Merge attempt', offset: 80 },
    ],
  },
  {
    name: 'documents',
    path: `/${domain}/documents`,
    ready: 'text=contract-c-3025-kavvik-filtration.md',
    steps: [{ waitFor: 'text=scan-contract-1999.pdf' }, { wait: 300 }],
  },
  {
    name: 'review-items',
    path: `/${domain}/review/44?tab=items`,
    ready: '[role=grid] tr[aria-selected]',
    steps: [
      { click: '[role=grid] tr:has-text("Contract C-3025") button:has-text("Accept")' },
      { click: '[role=grid] tr:has-text("with_supplier: C-3025") button:has-text("Accept")' },
      { click: '[role=grid] tr:has-text("S-1013 to PL-14")' },
      { wait: 400 },
      { scrollToTop: '[role=tablist]', offset: 80 },
    ],
  },
  {
    name: 'settings',
    path: `/${domain}/settings`,
    ready: 'text=Appearance',
  },
  {
    name: 'explorer-home-dark',
    path: `/${domain}/explorer`,
    ready: 'text=Recent Revisions',
    theme: 'dark',
  },
];

/**
 * The same pictures from a live run: the Supplier and Record of the loaded sample, and the
 * Proposal the setup left open with two Conflicts. Content differs from the mock where the
 * sample data does (names, Revisions); the screens are the same.
 */
const LIVE_SHOTS = (domain: string, open: string): Shot[] => [
  { name: 'explorer-home', path: `/${domain}/explorer`, ready: 'text=Recent Revisions' },
  { name: 'explorer-kind', path: `/${domain}/explorer/Supplier`, ready: 'text=S-1001' },
  {
    name: 'explorer-record',
    path: `/${domain}/explorer/Supplier/S-1001`,
    ready: 'text=Kavsen Alloys',
  },
  {
    name: 'canvas-graph',
    path: `/${domain}/canvas?seed=Supplier%2FS-1001`,
    ready: '[role=img][aria-label^="Records graph"]',
    needsWebGL: true,
  },
  {
    name: 'canvas-schema',
    path: `/${domain}/canvas?view=schema`,
    ready: '[role=img][aria-label^="Schema graph"]',
    needsWebGL: true,
  },
  { name: 'modeler-list', path: `/${domain}/modeler`, ready: 'text=Supplier' },
  { name: 'modeler-edit', path: `/${domain}/modeler?view=edit`, ready: '.cm-content' },
  { name: 'review-list', path: `/${domain}/review`, ready: 'text=ERP sync' },
  {
    name: 'review-conflict',
    path: `/${domain}/review/${open}`,
    ready: '[role=listbox][aria-label="Conflicts"] [role=option]',
    steps: [
      { click: '[role=listbox][aria-label="Conflicts"] [role=option]' },
      { wait: 400 },
      { scrollToTop: 'text=Merge attempt', offset: 80 },
    ],
  },
  { name: 'settings', path: `/${domain}/settings`, ready: 'text=Appearance' },
  {
    name: 'explorer-home-dark',
    path: `/${domain}/explorer`,
    ready: 'text=Recent Revisions',
    theme: 'dark',
  },
];

/** Ids the demo pictures need, read from the running stack (GET only). */
interface DemoIds {
  /** An open extraction Proposal, for the items picture. */
  extraction: string;
  /** A guide session, for the Agent activity picture. */
  session: string;
}

/** A question the guide answers from the sample data, for the Explore picture. */
const DEMO_QUESTION = 'Which parts depend on a single supplier, and how risky is that supplier?';

/**
 * The pictures of a running demo stack, read only: real data, the guide's real answer and its
 * trace. Nothing is written to the Domain: previews and searches are reads.
 */
const DEMO_SHOTS = (domain: string, ids: DemoIds): Shot[] => [
  { name: 'explorer-home', path: `/${domain}/explorer`, ready: 'text=Recent Revisions' },
  { name: 'explorer-kind', path: `/${domain}/explorer/Supplier`, ready: 'text=S-1001' },
  {
    name: 'explorer-record',
    path: `/${domain}/explorer/Supplier/S-1001`,
    ready: 'table[aria-label="Lineage of each value"], [aria-label="Record sections"]',
    steps: [{ wait: 600 }],
  },
  {
    name: 'record-history',
    path: `/${domain}/explorer/Supplier/S-1001`,
    ready: '[aria-label="Record sections"]',
    steps: [{ click: '[role=tab]:has-text("History")' }, { wait: 800 }],
  },
  {
    name: 'canvas-graph',
    path: `/${domain}/canvas?seed=Supplier%2FS-1001`,
    ready: '[role=img][aria-label^="Records graph"]',
    needsWebGL: true,
  },
  {
    name: 'canvas-schema',
    path: `/${domain}/canvas?view=schema`,
    ready: '[role=img][aria-label^="Schema graph"]',
    needsWebGL: true,
  },
  { name: 'modeler-list', path: `/${domain}/modeler`, ready: 'text=Supplier' },
  { name: 'modeler-edit', path: `/${domain}/modeler?view=edit`, ready: '.cm-content' },
  {
    name: 'modeler-operation-steps',
    path: `/${domain}/modeler?view=edit`,
    ready: '.cm-content',
    steps: [
      { click: '[role=tab]:has-text("Operations")' },
      { waitFor: 'text=Effects, in order.' },
      { scrollToTop: 'text=Effects, in order.', offset: 120 },
      { wait: 300 },
    ],
  },
  {
    name: 'modeler-derivation-preview',
    path: `/${domain}/modeler?view=edit`,
    ready: '.cm-content',
    steps: [
      { click: '[role=tab]:has-text("Derivations")' },
      { waitFor: 'table[aria-label^="Sample values"]' },
      { scrollToTop: 'table[aria-label^="Sample values"]', offset: 320 },
      { wait: 300 },
    ],
  },
  {
    name: 'modeler-source-preview',
    path: `/${domain}/modeler?view=edit`,
    ready: '.cm-content',
    steps: [
      { click: '[role=tab]:has-text("Sources")' },
      { upload: 'input[type=file]', files: ['vendors.csv'], from: 'sourceSample' },
      { waitFor: 'table[aria-label^="Sample rows"]' },
      { scrollToTop: 'table[aria-label^="Sample rows"]', offset: 260 },
      { wait: 300 },
    ],
  },
  {
    name: 'search-results',
    path: `/${domain}/retrieval-lab?q=brevik%20beerings&budget=1200`,
    ready: 'text=Corrected',
    steps: [
      { waitFor: 'text=Embedded by' },
      { wait: 800 },
      { scrollToTop: 'text=Results', offset: 60 },
    ],
  },
  {
    name: 'retrieval-lab',
    path: `/${domain}/retrieval-lab?q=brevik%20beerings&budget=1200`,
    ready: '[role=img][aria-label^="Involved Records"]',
    steps: [{ waitFor: 'text=Embedded by' }, { wait: 800 }],
    needsWebGL: true,
    height: 1700,
  },
  { name: 'review-list', path: `/${domain}/review`, ready: 'main >> text=Extract' },
  {
    name: 'review-items',
    path: `/${domain}/review/${ids.extraction}?tab=items`,
    ready: '[role=grid] tr[aria-selected]',
    steps: [{ wait: 400 }, { scrollToTop: '[role=tablist]', offset: 80 }],
  },
  {
    name: 'review-suggestions',
    path: `/${domain}/review?tab=suggestions`,
    ready: 'main table',
    steps: [{ wait: 800 }],
  },
  { name: 'documents', path: `/${domain}/documents`, ready: 'text=supplier-review-2025-q4.md' },
  {
    name: 'explore',
    path: `/${domain}/explore`,
    ready: 'textarea',
    steps: [
      { fill: 'textarea', text: DEMO_QUESTION },
      { click: 'form button[type=submit]' },
      { waitFor: '[aria-label="Answer"] >> text=View trace', timeoutMs: 240_000 },
      { click: 'text=Show cited Records on the Canvas' },
      { waitFor: '[role=img][aria-label^="Cited Records"]' },
      { wait: 1500 },
      // From the status line down: the steps, the answer and the cited Records.
      { scrollToTop: 'text=/^Done in/', offset: 12 },
    ],
    height: 1500,
  },
  {
    name: 'agent-activity',
    path: `/${domain}/agent-activity?session=${encodeURIComponent(ids.session)}`,
    ready: 'text=guide.query',
    steps: [
      {
        click:
          '[aria-label="Session timeline"] li:has-text("guide.query") [aria-label^="Show step"]',
      },
      { waitFor: '[role=img][aria-label^="Records touched"]' },
      { wait: 1500 },
      { scrollToTop: '[aria-label="Session timeline"]', offset: 160 },
    ],
    needsWebGL: true,
    graph: '[role=img][aria-label^="Records touched"]',
  },
  { name: 'settings', path: `/${domain}/settings`, ready: 'text=Appearance' },
  {
    name: 'explorer-home-dark',
    path: `/${domain}/explorer`,
    ready: 'text=Recent Revisions',
    theme: 'dark',
  },
];

/** The ids of DEMO_SHOTS, from the REST API of the demo stack. */
async function demoIds(keeper: string, domain: string): Promise<DemoIds> {
  const get = async (path: string): Promise<{ items: Record<string, unknown>[] }> => {
    const response = await fetch(`${keeper}/api/v1${path}`);
    if (!response.ok) throw new Error(`GET ${path}: ${response.status.toString()}`);
    return (await response.json()) as { items: Record<string, unknown>[] };
  };
  const proposals = await get(`/domains/${encodeURIComponent(domain)}/proposals`);
  const extraction = proposals.items.find(
    (p) => p['status'] === 'open' && p['origin'] === 'extraction',
  );
  const sessions = await get('/trace/sessions');
  const session = sessions.items.find(
    (s) => s['inferred'] === false && Array.isArray(s['domains']) && s['domains'].includes(domain),
  );
  if (extraction === undefined || session === undefined) {
    throw new Error('The demo stack needs an open extraction Proposal and a guide session.');
  }
  return { extraction: String(extraction['id']), session: String(session['session_id']) };
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

/** `keeper` set: the app in live mode, with a same-origin proxy to that Keeper. */
async function startServer(
  keeper: string | null,
  guide: string | null = null,
): Promise<{ server: PreviewServer; origin: string }> {
  const root = resolve(import.meta.dirname, '..');
  const outDir = keeper === null ? CONFIG.buildDir : CONFIG.liveBuildDir;
  // Vite gives process variables precedence over .env files.
  process.env['VITE_API_MODE'] = keeper === null ? CONFIG.apiMode : 'live';
  if (keeper !== null) process.env['VITE_KEEPER_BASE_URL'] = '/';
  if (guide === null) delete process.env['VITE_GUIDE_URL'];
  else process.env['VITE_GUIDE_URL'] = '/guide';
  await build({
    root,
    logLevel: 'warn',
    build: { outDir, emptyOutDir: true },
  });
  const port = await freePort();
  const server = await preview({
    root,
    logLevel: 'warn',
    build: { outDir },
    preview: {
      host: '127.0.0.1',
      port,
      strictPort: true,
      proxy: {
        ...(keeper === null ? {} : { '/api': { target: keeper } }),
        // The guide service checks the Origin: present the proxied calls as its own.
        ...(guide === null
          ? {}
          : {
              '/guide': {
                target: guide,
                changeOrigin: true,
                headers: { origin: new URL(guide).origin },
              },
            }),
      },
    },
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
      .waitFor({ state: 'visible', timeout: step.timeoutMs ?? CONFIG.readyTimeoutMs });
  } else if ('fill' in step) {
    await page.locator(step.fill).first().fill(step.text);
  } else if ('check' in step) {
    await page.locator(step.check).nth(step.nth).check();
  } else if ('upload' in step) {
    const dir = step.from === 'sourceSample' ? CONFIG.sourceSampleDir : CONFIG.sampleDataDir;
    const files = step.files.map((name) => resolve(dir, name));
    await page.locator(step.upload).setInputFiles(files);
  } else {
    await page.waitForTimeout(step.wait);
  }
}

/**
 * True when a Sigma canvas exists inside the graph container. The layout chip is a status too,
 * so a status alone does not mean the list fallback was drawn.
 */
async function canvasDrew(page: Page, ready: string): Promise<boolean> {
  return (await page.locator(ready).first().locator('canvas').count()) > 0;
}

async function capture(browser: Browser, origin: string, shot: Shot): Promise<string> {
  const theme = shot.theme ?? CONFIG.defaultTheme;
  const context = await browser.newContext({
    viewport: { ...CONFIG.viewport, height: shot.height ?? CONFIG.viewport.height },
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
    if (shot.needsWebGL === true && !(await canvasDrew(page, shot.graph ?? shot.ready))) {
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
 * The scripted walk behind docs/images/tour.gif: Explorer, a Record, the Canvas, the Source
 * mapping and its sample preview in the Modeler, and the Review resolver on Proposal 41. Every step reads like a user action; waits let the picture settle.
 */
const FLOW = {
  start: `/${MOCK_DOMAIN}/explorer`,
  ready: 'text=Recent Revisions',
  viewport: { width: 1280, height: 800 },
  output: resolve(import.meta.dirname, '../../../docs/images/tour.gif'),
  /** GIF width in pixels, frame rate and palette size: these decide the file size. */
  gif: { width: 800, fps: 8, colors: 80 },
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
    { click: '[aria-label="Main"] >> text=Modeler' },
    { click: '[role=tab]:has-text("Edit schema")' },
    { waitFor: '.cm-content' },
    { click: '[role=tab]:has-text("Sources")' },
    { waitFor: 'text=Transforms of legal_name' },
    { scrollToTop: 'text=Attribute mapping', offset: 120 },
    { wait: 1800 },
    { upload: 'input[type=file]', files: ['vendors.csv'], from: 'sourceSample' },
    { waitFor: 'table[aria-label^="Sample rows"]' },
    { scrollToTop: 'table[aria-label^="Sample rows"]', offset: 260 },
    { wait: 2200 },
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

/** `--name value` or `--name=value` from the command line, or `undefined`. */
function option(name: string): string | undefined {
  const args = process.argv.slice(2);
  const index = args.findIndex((arg) => arg === `--${name}` || arg.startsWith(`--${name}=`));
  const arg = args[index];
  if (arg === undefined) return undefined;
  return arg.includes('=') ? arg.slice(arg.indexOf('=') + 1) : args[index + 1];
}

const flag = (name: string) => process.argv.slice(2).includes(`--${name}`);

interface Target {
  /** Keeper to proxy to, `null` for the mock. */
  keeper: string | null;
  /** Guide service to proxy to under /guide, or `null`. */
  guide: string | null;
  shots: Shot[];
  /** Runs after the pictures, to clean up what the target loaded. */
  done: () => Promise<void>;
}

/** The mock needs nothing; a live run loads its Domain and Proposals into the Keeper first. */
async function target(): Promise<Target> {
  if (!flag('live')) {
    return {
      keeper: null,
      guide: null,
      shots: MOCK_SHOTS(MOCK_DOMAIN),
      done: () => Promise.resolve(),
    };
  }
  const url = option('keeper') ?? process.env['LOREBASE_E2E_URL'];
  if (url === undefined || url === '') {
    throw new Error(
      '--live needs a running Keeper: --keeper http://127.0.0.1:8080 (or LOREBASE_E2E_URL).',
    );
  }
  await waitForKeeper(url, 30_000);
  const base = url.replace(/\/+$/, '');
  const guide = option('guide')?.replace(/\/+$/, '') ?? null;
  const existing = option('domain');
  if (existing !== undefined) {
    // A demo stack, read only: its Domain as it is, nothing loaded or dropped.
    const ids = await demoIds(base, existing);
    console.log(`live: Domain ${existing} of ${url}, read only`);
    return {
      keeper: base,
      guide,
      shots: DEMO_SHOTS(existing, ids),
      done: () => Promise.resolve(),
    };
  }
  const keeper = createKeeper(url);
  const domain = `tour-${Date.now().toString(36)}`;
  await loadSupply(keeper, domain);
  const { open } = await prepareConflicts(keeper, domain);
  console.log(`live: Domain ${domain} loaded into ${url}`);
  return {
    keeper: base,
    guide,
    shots: LIVE_SHOTS(domain, open),
    done: () => (flag('keep') ? Promise.resolve() : dropDomain(keeper, domain)),
  };
}

async function main(): Promise<void> {
  const flow = process.argv[2] === 'flow';
  if (flow && flag('live')) {
    throw new Error('The walk behind tour.gif is recorded in mock mode; drop --live.');
  }
  await mkdir(CONFIG.outputDir, { recursive: true });
  const { keeper, guide, shots: all, done } = await target();
  const only = option('only')?.split(',');
  const shots = only === undefined ? all : all.filter((shot) => only.includes(shot.name));
  const { server, origin } = await startServer(keeper, guide);
  const browser = await chromium.launch({ args: CONFIG.browserArgs });
  try {
    if (flow) {
      await recordFlow(browser, origin);
    } else {
      const failed: string[] = [];
      for (const shot of shots) {
        try {
          console.log(`${shot.name}: ${await capture(browser, origin, shot)}`);
        } catch (error) {
          failed.push(shot.name);
          console.error(
            `${shot.name}: failed: ${error instanceof Error ? error.message : String(error)}`,
          );
        }
      }
      if (failed.length > 0) throw new Error(`Pictures not taken: ${failed.join(', ')}`);
    }
  } finally {
    await browser.close();
    await server.close();
    await done();
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
