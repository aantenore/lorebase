import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { SchemaDocument } from './document';
import { renderForm } from './form-harness';
import { SourcesForm } from './source-form';

const document = (): SchemaDocument => ({
  kinds: {
    Supplier: {
      primary_key: ['code'],
      attributes: {
        code: { type: 'string' },
        legal_name: { type: 'string' },
        country: { type: 'string' },
        risk_score: { type: 'integer', derivation: 'risk' },
      },
    },
    Plant: { primary_key: ['code'], attributes: { code: { type: 'string' } } },
  },
  relations: {
    feeds: { inverse_api_name: 'fed_by', from_kind: 'Supplier', to_kind: 'Plant' },
  },
  sources: {
    'approved.suppliers': {
      id: 'src_approved',
      type: 'csv',
      connection: 'secret://env/APPROVED_CSV',
      target_kind: 'Supplier',
      key_mapping: ['supplier_code'],
      attribute_mapping: {
        legal_name: { column: 'legal_name', transforms: [{ name: 'trim' }] },
      },
      sync_mode: 'snapshot',
      target_branch_policy: { branch_template: 'source/{source}', merge_policy: 'auto_when_clean' },
      x_contact: 'procurement',
    },
  },
});

const source = (doc: SchemaDocument) => doc.sources?.['approved.suppliers'] ?? {};

describe('Source editor', () => {
  it('edits a column mapping and keeps transforms and unknown keys', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(SourcesForm, document());
    const column = screen.getByRole('textbox', { name: 'Column for legal_name' });
    expect(column).toHaveAccessibleDescription('Transforms (edited in YAML): trim.');
    await user.clear(column);
    await user.type(column, 'name');
    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Mapping of country' }),
      'column',
    );
    expect(source(current())['attribute_mapping']).toEqual({
      legal_name: { column: 'name', transforms: [{ name: 'trim' }] },
      country: { column: 'country' },
    });
    const before = source(document());
    for (const key of ['id', 'connection', 'key_mapping', 'target_branch_policy', 'x_contact']) {
      expect(source(current())[key]).toEqual(before[key]);
    }
    // A derived Attribute cannot be mapped, so it is not offered.
    expect(screen.queryByRole('combobox', { name: 'Mapping of risk_score' })).toBeNull();
  });

  it('accepts only secret references for the connection', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(SourcesForm, document());
    const connection = screen.getByRole('textbox', { name: 'Connection' });
    await user.clear(connection);
    await user.type(connection, 'postgres://erp:hunter2@db/erp');
    expect(connection).toHaveAttribute('aria-invalid', 'true');
    expect(connection).toHaveAccessibleDescription(
      'Must be a secret:// reference, never a literal.',
    );
    await user.clear(connection);
    await user.type(connection, 'secret://env/ERP_DSN');
    expect(connection).not.toHaveAttribute('aria-invalid');
    expect(source(current())['connection']).toBe('secret://env/ERP_DSN');
  });

  it('shows the fields of each type and drops the ones a new type does not take', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(SourcesForm, document());
    expect(screen.queryByRole('textbox', { name: 'Records path' })).toBeNull();
    await user.selectOptions(screen.getByRole('combobox', { name: 'Source type' }), 'http_json');
    await user.type(screen.getByRole('textbox', { name: 'Records path' }), 'data.items');
    await user.type(screen.getByRole('textbox', { name: 'Flatten separator' }), '__');

    await user.click(screen.getByRole('button', { name: 'Add header' }));
    const header = screen.getByRole('textbox', { name: 'Header 1 name' });
    await user.clear(header);
    await user.type(header, 'Authorization');
    const value = screen.getByRole('textbox', { name: 'Header 1 value' });
    await user.type(value, 'Bearer abc');
    expect(value).toHaveAccessibleDescription('Must be a secret:// reference, never a literal.');
    await user.clear(value);
    await user.type(value, 'secret://env/API_TOKEN');
    expect(value).not.toHaveAttribute('aria-invalid');

    await user.selectOptions(screen.getByRole('combobox', { name: 'Pagination' }), 'cursor');
    await user.type(screen.getByRole('textbox', { name: 'Cursor path' }), 'meta.next');
    await user.type(screen.getByRole('spinbutton', { name: 'Page size' }), '100');
    await user.type(screen.getByRole('spinbutton', { name: 'Max pages per run' }), '50');
    expect(source(current())).toMatchObject({
      type: 'http_json',
      records_path: 'data.items',
      flatten_separator: '__',
      http: {
        headers: { Authorization: 'secret://env/API_TOKEN' },
        pagination: { type: 'cursor', cursor_path: 'meta.next', page_size: 100 },
        max_pages: 50,
      },
    });

    await user.selectOptions(screen.getByRole('combobox', { name: 'Source type' }), 'jsonl');
    const after = source(current());
    expect(after['http']).toBeUndefined();
    expect(after['records_path']).toBeUndefined();
    expect(after['flatten_separator']).toBe('__');
    expect(after['x_contact']).toBe('procurement');
  });

  it('sets sync mode, watermark, relation columns and merge policy', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(SourcesForm, document());
    await user.selectOptions(screen.getByRole('combobox', { name: 'Sync mode' }), 'incremental');
    await user.type(screen.getByRole('textbox', { name: 'Watermark column' }), 'updated_at');
    await user.type(screen.getByRole('textbox', { name: 'Columns for feeds' }), 'plant_code');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Merge policy' }), 'proposal');
    expect(source(current())).toMatchObject({
      sync_mode: 'incremental',
      watermark_column: 'updated_at',
      relation_mapping: [{ relation: 'feeds', columns: ['plant_code'] }],
      target_branch_policy: { branch_template: 'source/{source}', merge_policy: 'proposal' },
    });
  });

  it('shows validation errors next to their fields', () => {
    renderForm(SourcesForm, document(), [
      {
        path: 'sources/approved.suppliers/key_mapping',
        code: 'key_mapping_length',
        message: 'The key mapping has 2 columns, the primary key of Supplier has 1.',
      },
      {
        path: 'sources/approved.suppliers/watermark_column',
        code: 'missing_watermark',
        message: 'An incremental Source needs a watermark column.',
      },
    ]);
    expect(
      screen.getByText('The key mapping has 2 columns, the primary key of Supplier has 1.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Watermark column' })).toHaveAccessibleDescription(
      'An incremental Source needs a watermark column.',
    );
    const key = screen.getByRole('group', { name: 'Key mapping' });
    expect(within(key).getByRole('textbox', { name: 'Key column for code' })).toHaveValue(
      'supplier_code',
    );
  });
});
