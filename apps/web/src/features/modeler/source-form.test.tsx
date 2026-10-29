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
        legal_name: { type: 'string', valid_time: true },
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
    expect(screen.getByRole('combobox', { name: 'legal_name transform 1 name' })).toHaveValue(
      'trim',
    );
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

  it('names the validity columns of a valid_time Attribute and drops them when cleared', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(SourcesForm, document());
    expect(screen.queryByRole('textbox', { name: 'Valid from for country' })).toBeNull();
    await user.type(screen.getByRole('textbox', { name: 'Valid from for legal_name' }), 'since');
    await user.type(screen.getByRole('textbox', { name: 'Valid to for legal_name' }), 'until');
    expect(source(current())['attribute_mapping']).toEqual({
      legal_name: {
        column: 'legal_name',
        transforms: [{ name: 'trim' }],
        valid_from: 'since',
        valid_to: 'until',
      },
    });
    await user.clear(screen.getByRole('textbox', { name: 'Valid to for legal_name' }));
    expect(source(current())['attribute_mapping']).toEqual({
      legal_name: { column: 'legal_name', transforms: [{ name: 'trim' }], valid_from: 'since' },
    });
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

  it('offers the MySQL and SQL Server types and keeps their definition on a round trip', async () => {
    const user = userEvent.setup();
    const doc = document();
    Object.assign(source(doc), {
      type: 'mysql_table',
      connection: 'secret://env/ERP_MYSQL',
      table: 'erp.orders',
    });
    const { current } = renderForm(SourcesForm, doc);
    const type = screen.getByRole('combobox', { name: 'Source type' });
    expect(type).toHaveValue('mysql_table');
    expect(screen.getByRole('textbox', { name: 'Table' })).toHaveValue('erp.orders');
    expect(screen.queryByRole('textbox', { name: 'Query' })).toBeNull();
    expect(source(current())).toEqual(source(doc));

    await user.selectOptions(type, 'mssql_query');
    expect(source(current())['table']).toBeUndefined();
    await user.type(screen.getByRole('textbox', { name: 'Query' }), 'SELECT 1 AS a');
    expect(source(current())).toMatchObject({
      type: 'mssql_query',
      connection: 'secret://env/ERP_MYSQL',
      query: 'SELECT 1 AS a',
      x_contact: 'procurement',
    });

    await user.selectOptions(type, 'mssql_table');
    expect(source(current())['query']).toBeUndefined();
    expect(screen.getByRole('textbox', { name: 'Table' })).toBeInTheDocument();
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

  it('sets what an upstream change does to local edits, review being the default', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(SourcesForm, document());
    const localEdits = screen.getByRole('combobox', { name: 'Local edits' });
    expect(localEdits).toHaveValue('review');
    await user.selectOptions(localEdits, 'keep');
    expect(source(current())['local_edits']).toBe('keep');
    await user.selectOptions(localEdits, 'review');
    expect(source(current())).not.toHaveProperty('local_edits');
  });

  it('sets stale_after, an empty field leaving it out', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(SourcesForm, document());
    const staleAfter = screen.getByRole('textbox', { name: 'Stale after' });
    await user.type(staleAfter, 'P30D');
    expect(source(current())['stale_after']).toBe('P30D');
    await user.clear(staleAfter);
    expect(source(current())).not.toHaveProperty('stale_after');
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
