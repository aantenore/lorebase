/**
 * The bundle budget: size limits for the JavaScript a first visit downloads, checked against
 * a production build by `scripts/check-bundle-budget.ts` (part of `pnpm build`). The limits
 * live in `bundle-budget.json`. Sizes are in kB of 1000 bytes, as Vite reports them.
 */

export interface SizeLimit {
  /** Largest allowed size of the file as served. */
  maxKb: number;
  /** Largest allowed size after gzip. */
  maxGzipKb: number;
}

export interface BundleBudget {
  /** The entry chunk named by `index.html`. */
  mainChunk: SizeLimit;
  /** The entry chunk plus every chunk `index.html` preloads: all JavaScript of the first load. */
  initialJs: SizeLimit;
}

export interface ChunkSize {
  file: string;
  bytes: number;
  gzipBytes: number;
}

export interface InitialScripts {
  entry: string;
  preloads: string[];
}

function isSizeLimit(value: unknown): value is SizeLimit {
  if (typeof value !== 'object' || value === null) return false;
  const limit = value as Record<string, unknown>;
  return (
    typeof limit['maxKb'] === 'number' &&
    limit['maxKb'] > 0 &&
    typeof limit['maxGzipKb'] === 'number' &&
    limit['maxGzipKb'] > 0
  );
}

/** Reads the parsed content of `bundle-budget.json`; throws when it is malformed. */
export function parseBudget(raw: unknown): BundleBudget {
  const budget = raw as Record<string, unknown> | null;
  const mainChunk = budget?.['mainChunk'];
  const initialJs = budget?.['initialJs'];
  if (!isSizeLimit(mainChunk) || !isSizeLimit(initialJs)) {
    throw new Error(
      'bundle-budget.json needs "mainChunk" and "initialJs", each with positive "maxKb" and "maxGzipKb".',
    );
  }
  return {
    mainChunk: { maxKb: mainChunk.maxKb, maxGzipKb: mainChunk.maxGzipKb },
    initialJs: { maxKb: initialJs.maxKb, maxGzipKb: initialJs.maxGzipKb },
  };
}

/** The entry script and the preloaded chunks of a built `index.html`, as served paths. */
export function initialScripts(html: string): InitialScripts {
  const entry = /<script\b[^>]*\btype="module"[^>]*\bsrc="([^"]+)"/.exec(html)?.[1];
  if (entry === undefined) throw new Error('index.html has no module entry script.');
  const preloads = [
    ...html.matchAll(/<link\b[^>]*\brel="modulepreload"[^>]*\bhref="([^"]+)"/g),
  ].flatMap((match) => (match[1] === undefined ? [] : [match[1]]));
  return { entry, preloads };
}

const kb = (bytes: number) => bytes / 1000;

function over(label: string, bytes: number, gzipBytes: number, limit: SizeLimit): string[] {
  const problems: string[] = [];
  if (kb(bytes) > limit.maxKb) {
    problems.push(`${label} is ${kb(bytes).toFixed(2)} kB, over the ${limit.maxKb} kB budget.`);
  }
  if (kb(gzipBytes) > limit.maxGzipKb) {
    problems.push(
      `${label} is ${kb(gzipBytes).toFixed(2)} kB gzipped, over the ${limit.maxGzipKb} kB budget.`,
    );
  }
  return problems;
}

/** Every way the build exceeds the budget; empty when it is within it. */
export function budgetViolations(
  budget: BundleBudget,
  main: ChunkSize,
  initial: ChunkSize[],
): string[] {
  const total = initial.reduce(
    (sum, chunk) => ({
      bytes: sum.bytes + chunk.bytes,
      gzipBytes: sum.gzipBytes + chunk.gzipBytes,
    }),
    { bytes: 0, gzipBytes: 0 },
  );
  return [
    ...over(`The main chunk (${main.file})`, main.bytes, main.gzipBytes, budget.mainChunk),
    ...over('The JavaScript of the first load', total.bytes, total.gzipBytes, budget.initialJs),
  ];
}
