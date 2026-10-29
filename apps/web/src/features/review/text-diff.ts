export type DiffOp = 'equal' | 'insert' | 'delete';

export interface DiffPart {
  op: DiffOp;
  text: string;
}

/**
 * Largest table (tokens of the base times tokens of the other side) the diff will fill.
 * Past it the caller shows the whole value as changed instead of a word diff.
 */
export const MAX_DIFF_CELLS = 250_000;

/** Words and the whitespace between them, each as its own token. */
export function tokenize(value: string): string[] {
  return value.split(/(\s+)/).filter((token) => token !== '');
}

function push(parts: DiffPart[], op: DiffOp, text: string) {
  const last = parts[parts.length - 1];
  if (last?.op === op) last.text += text;
  else parts.push({ op, text });
}

/**
 * Changes separated only by whitespace become one removed run and one added run (removed
 * first), the whitespace between them kept in both, so "si muore." reads as one phrase.
 */
function coalesce(parts: DiffPart[]): DiffPart[] {
  const out: DiffPart[] = [];
  let removed = '';
  let added = '';
  const flush = () => {
    if (removed !== '') out.push({ op: 'delete', text: removed });
    if (added !== '') out.push({ op: 'insert', text: added });
    removed = '';
    added = '';
  };
  parts.forEach((part, index) => {
    const pending = removed !== '' || added !== '';
    const between =
      part.op === 'equal' && pending && part.text.trim() === '' && parts[index + 1] !== undefined;
    if (part.op === 'delete') removed += part.text;
    else if (part.op === 'insert') added += part.text;
    else if (between) {
      removed += part.text;
      added += part.text;
    } else {
      flush();
      out.push(part);
    }
  });
  flush();
  return out;
}

/**
 * Word-level changes from `base` to `side`: tokens only in the base are `delete`, tokens
 * only in the side are `insert`, in reading order. `null` when the table would pass
 * `MAX_DIFF_CELLS` or when the two share no word, where a diff would only add noise.
 */
export function wordDiff(base: string, side: string): DiffPart[] | null {
  const a = tokenize(base);
  const b = tokenize(side);
  const width = b.length + 1;
  if ((a.length + 1) * width > MAX_DIFF_CELLS) return null;

  // lcs[i * width + j]: longest common subsequence of a[i..] and b[j..].
  const lcs = new Uint32Array((a.length + 1) * width);
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      lcs[i * width + j] =
        a[i] === b[j]
          ? (lcs[(i + 1) * width + j + 1] ?? 0) + 1
          : Math.max(lcs[(i + 1) * width + j] ?? 0, lcs[i * width + j + 1] ?? 0);
    }
  }

  const parts: DiffPart[] = [];
  let shared = false;
  let i = 0;
  let j = 0;
  while (i < a.length || j < b.length) {
    const left = a[i];
    const right = b[j];
    if (left !== undefined && left === right) {
      shared ||= left.trim() !== '';
      push(parts, 'equal', left);
      i += 1;
      j += 1;
    } else if (
      right === undefined ||
      (left !== undefined && (lcs[(i + 1) * width + j] ?? 0) >= (lcs[i * width + j + 1] ?? 0))
    ) {
      push(parts, 'delete', left ?? '');
      i += 1;
    } else {
      push(parts, 'insert', right);
      j += 1;
    }
  }
  return shared ? coalesce(parts) : null;
}
