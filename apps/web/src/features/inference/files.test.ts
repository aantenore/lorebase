import { loadConfig } from '@/config/env';
import { addFiles, formatBytes, type UploadCaps } from './files';

const caps: UploadCaps = {
  maxFiles: 3,
  maxFileBytes: 100,
  maxTotalBytes: 150,
  fileSuffixes: ['.csv', '.tsv'],
};

const file = (name: string, size: number) => new File(['x'.repeat(size)], name);

describe('addFiles', () => {
  it('takes the JSON, JSON Lines and Parquet files of the default suffixes', () => {
    const defaults = loadConfig({ VITE_API_MODE: 'mock' }).inference;
    const names = ['a.json', 'b.JSONL', 'c.ndjson', 'd.parquet'];
    const result = addFiles(
      [],
      names.map((name) => file(name, 10)),
      { ...defaults, maxTotalBytes: 100 },
    );
    expect(result.files.map((f) => f.name)).toEqual(names);
    expect(result.rejected).toEqual([]);
  });

  it('takes files within the caps', () => {
    const result = addFiles([], [file('a.csv', 10), file('B.TSV', 10)], caps);
    expect(result.files.map((f) => f.name)).toEqual(['a.csv', 'B.TSV']);
    expect(result.rejected).toEqual([]);
  });

  it('refuses by suffix, duplicate name, size, total size and count, with the reason', () => {
    const current = [file('a.csv', 90)];
    const result = addFiles(
      current,
      [
        file('notes.pdf', 1),
        file('a.csv', 1),
        file('huge.csv', 101),
        file('b.csv', 80),
        file('c.csv', 40),
        file('d.csv', 5),
      ],
      caps,
    );
    expect(Object.fromEntries(result.rejected.map((r) => [r.name, r.reason]))).toEqual({
      'notes.pdf': 'Only .csv, .tsv files are accepted.',
      'a.csv': 'A file with this name is already added.',
      'huge.csv': 'Larger than 100 B (101 B).',
      'b.csv': 'The files together would exceed 150 B.',
    });
    expect(result.files.map((f) => f.name)).toEqual(['a.csv', 'c.csv', 'd.csv']);
  });

  it('stops at the file count', () => {
    const result = addFiles(
      [file('a.csv', 1), file('b.csv', 1), file('c.csv', 1)],
      [file('d.csv', 1)],
      caps,
    );
    expect(result.rejected[0]?.reason).toBe('At most 3 files per run.');
  });
});

describe('formatBytes', () => {
  it('uses binary units', () => {
    expect(formatBytes(10)).toBe('10 B');
    expect(formatBytes(1024)).toBe('1 KiB');
    expect(formatBytes(1536)).toBe('1.5 KiB');
    expect(formatBytes(1_048_576)).toBe('1 MiB');
  });
});
