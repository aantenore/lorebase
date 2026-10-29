import { MAX_DIFF_CELLS, tokenize, wordDiff } from './text-diff';

describe('tokenize', () => {
  it('keeps the whitespace between words as tokens', () => {
    expect(tokenize('a  b\nc')).toEqual(['a', '  ', 'b', '\n', 'c']);
    expect(tokenize('')).toEqual([]);
  });
});

describe('wordDiff', () => {
  it('marks the words added and removed against the base', () => {
    expect(wordDiff('Monza Foundry S.r.l.', 'Monza Foundry')).toEqual([
      { op: 'equal', text: 'Monza Foundry' },
      { op: 'delete', text: ' S.r.l.' },
    ]);
    expect(wordDiff('the quick fox', 'the slow fox')).toEqual([
      { op: 'equal', text: 'the ' },
      { op: 'delete', text: 'quick' },
      { op: 'insert', text: 'slow' },
      { op: 'equal', text: ' fox' },
    ]);
  });

  it('joins changes separated only by whitespace into one removed and one added run', () => {
    expect(wordDiff('x si muore. y', 'x la persona y')).toEqual([
      { op: 'equal', text: 'x ' },
      { op: 'delete', text: 'si muore.' },
      { op: 'insert', text: 'la persona' },
      { op: 'equal', text: ' y' },
    ]);
    // Words kept between two changes still split them.
    expect(wordDiff('a b c d e', 'a x c y e')?.map((p) => p.op)).toEqual([
      'equal',
      'delete',
      'insert',
      'equal',
      'delete',
      'insert',
      'equal',
    ]);
  });

  it('rebuilds both texts from the parts', () => {
    const cases: [string, string][] = [
      ['one two three four five', 'one three four six seven'],
      ['x si muore.  e\nfine', 'x la persona e\nperduta'],
    ];
    for (const [base, side] of cases) expectRebuilt(base, side);
  });

  function expectRebuilt(base: string, side: string) {
    const parts = wordDiff(base, side) ?? [];
    const join = (ops: string[]) =>
      parts
        .filter((p) => ops.includes(p.op))
        .map((p) => p.text)
        .join('');
    expect(join(['equal', 'delete'])).toBe(base);
    expect(join(['equal', 'insert'])).toBe(side);
  }

  it('gives no diff when nothing but whitespace is shared', () => {
    expect(wordDiff('2', '3')).toBeNull();
    expect(wordDiff('a b', 'c d')).toBeNull();
  });

  it('gives no diff when the table would be too large', () => {
    const words = (n: number) => Array.from({ length: n }, (_, i) => `w${i.toString()}`).join(' ');
    const size = Math.ceil(Math.sqrt(MAX_DIFF_CELLS));
    expect(wordDiff(words(size), `${words(size)} extra`)).toBeNull();
    expect(wordDiff(words(10), `${words(10)} extra`)).not.toBeNull();
  });
});
