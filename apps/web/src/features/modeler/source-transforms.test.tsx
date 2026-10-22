import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { asRemote, createMockApi } from '@/api/mock/adapter';
import {
  documentToSchema,
  schemaToDocument,
  type IdFactory,
  type SchemaDocument,
} from './document';
import { renderForm } from './form-harness';
import { SourcesForm } from './source-form';
import { dumpSchemaYaml, parseSchemaYaml } from './yaml';

const ALL = [
  { name: 'cast', args: { to: 'integer' } },
  { name: 'trim' },
  { name: 'lower' },
  { name: 'upper' },
  { name: 'parse_date', args: { format: '%d/%m/%Y' } },
  { name: 'concat', args: { columns: ['a', 'b'], separator: '-' } },
  { name: 'constant', args: { value: 42 } },
  { name: 'map', args: { values: { A: 'active', B: 1 }, default: 'unknown' } },
  { name: 'null_if', args: { values: ['n/a', 0] } },
];

const document = (transforms: unknown[] = []): SchemaDocument => ({
  kinds: {
    Supplier: {
      primary_key: ['code'],
      attributes: {
        code: { type: 'string' },
        legal_name: { type: 'string' },
        country: { type: 'string' },
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
        legal_name: {
          column: 'legal_name',
          transforms: structuredClone(transforms),
          x_note: 'kept',
        },
      },
      sync_mode: 'snapshot',
    },
  },
});

const mappingOf = (doc: SchemaDocument) =>
  (doc.sources?.['approved.suppliers']?.['attribute_mapping'] ?? {}) as Record<
    string,
    Record<string, unknown>
  >;

describe('Source transforms form', () => {
  it('shows every transform type and leaves the document alone until it is edited', () => {
    const { current } = renderForm(SourcesForm, document(ALL));
    ALL.forEach((transform, index) => {
      expect(
        screen.getByRole('combobox', { name: `legal_name transform ${index + 1} name` }),
      ).toHaveValue(transform.name);
    });
    expect(current()).toEqual(document(ALL));
  });

  it('edits the arguments in place and keeps the rest of the chain', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(SourcesForm, document(ALL));
    const field = (name: string) => screen.getByRole('textbox', { name });
    await user.selectOptions(
      screen.getByRole('combobox', { name: 'legal_name transform 1 target type' }),
      'decimal',
    );
    await user.clear(field('legal_name transform 5 date format'));
    await user.type(field('legal_name transform 5 date format'), '%Y');
    await user.type(field('legal_name transform 6 separator'), '+');
    await user.clear(field('legal_name transform 7 constant'));
    await user.type(field('legal_name transform 7 constant'), '43');
    await user.clear(field('legal_name transform 8 map 2 to'));
    await user.type(field('legal_name transform 8 map 2 to'), '2');
    await user.type(field('legal_name transform 9 values'), ', x');
    expect(mappingOf(current())['legal_name']).toEqual({
      column: 'legal_name',
      x_note: 'kept',
      transforms: [
        { name: 'cast', args: { to: 'decimal' } },
        ...ALL.slice(1, 4),
        { name: 'parse_date', args: { format: '%Y' } },
        { name: 'concat', args: { columns: ['a', 'b'], separator: '-+' } },
        { name: 'constant', args: { value: 43 } },
        { name: 'map', args: { values: { A: 'active', B: 2 }, default: 'unknown' } },
        { name: 'null_if', args: { values: ['n/a', 0, 'x'] } },
      ],
    });
  });

  it('adds, switches, reorders and removes transforms', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(SourcesForm, document());
    const chain = () => mappingOf(current())['legal_name']?.['transforms'];
    await user.click(screen.getByRole('button', { name: 'Add transform to legal_name' }));
    await user.click(screen.getByRole('button', { name: 'Add transform to legal_name' }));
    await user.selectOptions(
      screen.getByRole('combobox', { name: 'legal_name transform 2 name' }),
      'null_if',
    );
    expect(chain()).toEqual([{ name: 'trim' }, { name: 'null_if', args: { values: [] } }]);
    await user.type(screen.getByRole('textbox', { name: 'legal_name transform 2 values' }), 'n/a');
    await user.click(screen.getByRole('button', { name: 'Move legal_name transform 2 up' }));
    expect(chain()).toEqual([{ name: 'null_if', args: { values: ['n/a'] } }, { name: 'trim' }]);
    await user.click(screen.getByRole('button', { name: 'Remove legal_name transform 1' }));
    await user.click(screen.getByRole('button', { name: 'Remove legal_name transform 1' }));
    expect(chain()).toBeUndefined();
    expect(mappingOf(current())['legal_name']?.['x_note']).toBe('kept');
  });

  it('builds a map table from empty and survives a YAML round-trip', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(SourcesForm, document());
    await user.click(screen.getByRole('button', { name: 'Add transform to legal_name' }));
    await user.selectOptions(
      screen.getByRole('combobox', { name: 'legal_name transform 1 name' }),
      'map',
    );
    await user.click(screen.getByRole('button', { name: 'Add value' }));
    const from = screen.getByRole('textbox', { name: 'legal_name transform 1 map 1 from' });
    await user.clear(from);
    await user.type(from, 'IT');
    await user.type(
      screen.getByRole('textbox', { name: 'legal_name transform 1 map 1 to' }),
      'Italy',
    );
    expect(mappingOf(current())['legal_name']?.['transforms']).toEqual([
      { name: 'map', args: { values: { IT: 'Italy' } } },
    ]);
    const parsed = parseSchemaYaml(dumpSchemaYaml(current()));
    expect(parsed.ok).toBe(true);
    if (parsed.ok) expect(parsed.document).toEqual(current());
  });

  it('round-trips every transform type through the schema model', async () => {
    const api = asRemote(createMockApi({ pageSize: 50, searchLimit: 20, maxNodes: 200 }), 0);
    const schema = await api.schema.get({ domain: 'supply', branch: 'main' });
    const doc = schemaToDocument(schema);
    const [name, found] = Object.entries(doc.sources ?? {})[0] ?? [];
    const mapping = found?.['attribute_mapping'] as Record<string, Record<string, unknown>>;
    const attribute = Object.keys(mapping)[0] ?? '';
    mapping[attribute] = { column: 'c', transforms: structuredClone(ALL) };
    const mint: IdFactory = (prefix, key) => `${prefix}_${key.replace(/\W+/g, '_')}`;
    const { schema: back, issues } = documentToSchema(doc, schema, mint);
    expect(issues).toEqual([]);
    const out = back.sources.find((s) => s.api_name === name);
    expect(
      Object.values(out?.attribute_mapping ?? {}).find((m) => m.column === 'c')?.transforms,
    ).toEqual(ALL);
    expect(schemaToDocument(back).sources?.[name ?? '']).toEqual(doc.sources?.[name ?? '']);
  });
});
