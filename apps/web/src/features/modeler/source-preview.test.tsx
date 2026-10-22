import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { SchemaDocument } from './document';
import { renderForm } from './form-harness';
import { SourcesForm } from './source-form';
import { applyTransform, mapRows, parseDate, parseDelimited, readSample } from './source-preview';

const OPTIONS = { recordsPath: '', flattenSeparator: '', limit: 5 };

describe('sample reading', () => {
  it('reads quoted CSV fields with delimiters, quotes and line breaks', () => {
    const rows = parseDelimited(
      'code,name\r\nS1,"Acme, Inc"\r\nS2,"say ""hi""\nthere"\r\n\r\n',
      ',',
    );
    expect(rows).toEqual([
      { code: 'S1', name: 'Acme, Inc' },
      { code: 'S2', name: 'say "hi"\nthere' },
    ]);
  });

  it('takes the first rows of each format', () => {
    const csv = readSample('a.csv', 'x\n1\n2\n3\n4\n5\n6\n', OPTIONS);
    expect(csv.rows.map((r) => r['x'])).toEqual(['1', '2', '3', '4', '5']);
    expect(readSample('a.tsv', 'x\ty\n1\t2\n', OPTIONS).rows).toEqual([{ x: '1', y: '2' }]);
    expect(readSample('a.jsonl', '{"x":1}\n\n{"x":2}\n', OPTIONS).rows).toEqual([
      { x: 1 },
      { x: 2 },
    ]);
    const json = readSample('a.json', '{"data":{"items":[{"a":{"b":1}}]}}', {
      ...OPTIONS,
      recordsPath: 'data.items',
      flattenSeparator: '__',
    });
    expect(json.rows).toEqual([{ a__b: 1 }]);
  });

  it('explains what it cannot read', () => {
    expect(readSample('a.json', '{"x":1}', OPTIONS).error).toMatch(/not an array/);
    expect(readSample('a.json', '{', OPTIONS).error).toBeDefined();
    expect(readSample('a.parquet', '', OPTIONS).error).toMatch(/CSV, TSV, JSON/);
  });
});

describe('mapping', () => {
  it('applies the transforms in order, as Scribe does', () => {
    const row = { first: ' Ada ', last: 'Lovelace' };
    expect(applyTransform({ name: 'trim' }, ' x ', row)).toBe('x');
    expect(applyTransform({ name: 'upper' }, 'x', row)).toBe('X');
    expect(applyTransform({ name: 'lower' }, 'X', row)).toBe('x');
    expect(
      applyTransform({ name: 'concat', args: { columns: ['last'], separator: ' ' } }, 'Ada', row),
    ).toBe('Ada Lovelace');
    expect(applyTransform({ name: 'constant', args: { value: 7 } }, 'x', row)).toBe(7);
    expect(applyTransform({ name: 'null_if', args: { values: ['n/a'] } }, 'n/a', row)).toBeNull();
    expect(applyTransform({ name: 'map', args: { values: { A: 'a' } } }, 'A', row)).toBe('a');
    expect(applyTransform({ name: 'map', args: { values: {}, default: 'z' } }, 'A', row)).toBe('z');
    expect(applyTransform({ name: 'cast', args: { to: 'integer' } }, '12.0', row)).toBe(12);
    expect(() => applyTransform({ name: 'cast', args: { to: 'integer' } }, 'x', row)).toThrow();
  });

  it('parses dates by format', () => {
    expect(parseDate('31/12/2025', '%d/%m/%Y')).toBe('2025-12-31');
    expect(parseDate('3 Mar 2024', '%d %b %Y')).toBe('2024-03-03');
    expect(() => parseDate('31/02/2025', '%d/%m/%Y')).toThrow();
    expect(() => parseDate('2025', '%d/%m/%Y')).toThrow();
  });

  it('maps rows to keys and Attributes and reports the cells that fail', () => {
    const source = {
      key_mapping: ['code'],
      attribute_mapping: {
        name: { column: 'name', transforms: [{ name: 'trim' }] },
        score: { column: 'score', transforms: [{ name: 'cast', args: { to: 'integer' } }] },
        note: { expression: 'a + b' },
      },
    };
    const types = { name: 'string', score: 'integer', note: 'string', unmapped: 'string' };
    const [ok, bad, noKey] = mapRows(
      [
        { code: 'S1', name: ' Acme ', score: '5' },
        { code: 'S2', name: 'Bolt', score: 'five' },
        { code: '', name: 'x' },
      ],
      source,
      types,
    );
    expect(ok?.key).toEqual({ value: 'S1' });
    expect(ok?.cells['name']).toEqual({ value: 'Acme' });
    expect(ok?.cells['score']).toEqual({ value: '5' });
    expect(ok?.cells['note']?.error).toMatch(/not read by Scribe/);
    expect(ok?.cells['unmapped']).toBeUndefined();
    expect(bad?.cells['score']?.error).toBe('five is not an integer');
    expect(noKey?.key.error).toBe('The key is empty.');
    expect(noKey?.cells['score']).toEqual({ value: null });
  });
});

const document = (): SchemaDocument => ({
  kinds: {
    Supplier: {
      primary_key: ['code'],
      attributes: {
        code: { type: 'string' },
        legal_name: { type: 'string' },
        risk_score: { type: 'integer' },
        tier: { type: 'string', derivation: 'tiering' },
      },
    },
  },
  sources: {
    'approved.suppliers': {
      id: 'src_approved',
      type: 'csv',
      connection: 'secret://env/APPROVED_CSV',
      target_kind: 'Supplier',
      key_mapping: ['supplier_code'],
      attribute_mapping: {
        legal_name: { column: 'name', transforms: [{ name: 'upper' }] },
        risk_score: { column: 'risk', transforms: [{ name: 'cast', args: { to: 'integer' } }] },
      },
      sync_mode: 'snapshot',
    },
  },
});

describe('Source preview', () => {
  it('maps a sample file onto the target Kind and follows edits of the mapping', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(SourcesForm, document());
    const file = new File(['supplier_code,name,risk\nS1,acme,7\nS2,bolt,high\n'], 'suppliers.csv', {
      type: 'text/csv',
    });
    await user.upload(screen.getByLabelText('Sample file for approved.suppliers'), file);
    const table = await screen.findByRole('table', { name: 'Sample rows of approved.suppliers' });
    const rows = within(table).getAllByRole('row');
    expect(within(rows[1] as HTMLElement).getByText('ACME')).toBeInTheDocument();
    expect(within(rows[1] as HTMLElement).getByText('7')).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByText('high is not an integer')).toBeInTheDocument();
    // The derived Attribute is not mappable, so it has no column.
    expect(within(table).queryByRole('columnheader', { name: 'tier' })).toBeNull();

    await user.click(screen.getByRole('button', { name: 'Remove legal_name transform 1' }));
    await waitFor(() => {
      expect(within(table).getByText('acme')).toBeInTheDocument();
    });
    expect(current().sources?.['approved.suppliers']?.['connection']).toBe(
      'secret://env/APPROVED_CSV',
    );
  });

  it('reports a file it cannot read', async () => {
    const user = userEvent.setup({ applyAccept: false });
    renderForm(SourcesForm, document());
    await user.upload(
      screen.getByLabelText('Sample file for approved.suppliers'),
      new File(['x'], 'data.parquet'),
    );
    expect(await screen.findByRole('alert')).toHaveTextContent(/CSV, TSV, JSON/);
  });
});
