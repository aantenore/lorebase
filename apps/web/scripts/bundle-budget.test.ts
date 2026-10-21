import {
  budgetViolations,
  initialScripts,
  parseBudget,
  type BundleBudget,
  type ChunkSize,
} from './bundle-budget.ts';

const budget: BundleBudget = {
  mainChunk: { maxKb: 100, maxGzipKb: 40 },
  initialJs: { maxKb: 300, maxGzipKb: 100 },
};

const chunk = (file: string, kb: number, gzipKb: number): ChunkSize => ({
  file,
  bytes: kb * 1000,
  gzipBytes: gzipKb * 1000,
});

describe('bundle budget', () => {
  it('accepts a build within the budget', () => {
    const main = chunk('/assets/index.js', 90, 30);
    expect(budgetViolations(budget, main, [main, chunk('/assets/react.js', 150, 50)])).toEqual([]);
  });

  it('flags a main chunk over its size or gzip limit', () => {
    const big = chunk('/assets/index.js', 101, 41);
    const problems = budgetViolations(budget, big, [big]);
    expect(problems).toHaveLength(2);
    expect(problems[0]).toContain('/assets/index.js');
    expect(problems[0]).toContain('100 kB');
    expect(problems[1]).toContain('gzipped');
  });

  it('flags a first load over budget even when the main chunk fits', () => {
    const main = chunk('/assets/index.js', 90, 30);
    const problems = budgetViolations(budget, main, [main, chunk('/assets/vendor.js', 250, 60)]);
    expect(problems).toEqual([expect.stringContaining('first load')]);
  });

  it('reads the entry and the preloads of a built page', () => {
    const html = `<head>
      <script type="module" crossorigin src="/assets/index-abc.js"></script>
      <link rel="modulepreload" crossorigin href="/assets/react-def.js">
      <link rel="stylesheet" crossorigin href="/assets/index-xyz.css">
    </head>`;
    expect(initialScripts(html)).toEqual({
      entry: '/assets/index-abc.js',
      preloads: ['/assets/react-def.js'],
    });
    expect(() => initialScripts('<html></html>')).toThrow('entry script');
  });

  it('rejects a malformed budget file', () => {
    expect(parseBudget(budget)).toEqual(budget);
    expect(() => parseBudget({ mainChunk: { maxKb: 100 } })).toThrow('bundle-budget.json');
    expect(() => parseBudget(null)).toThrow('bundle-budget.json');
  });
});
