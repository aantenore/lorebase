import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { messages } from '@/messages';
import type { KeeperApi } from '@/api/client';
import type { FilePreview } from '@/api/inference-types';
import type { SchemaDocument } from './document';
import { renderForm } from './form-harness';
import { SourcesForm } from './source-form';

const state = vi.hoisted(() => ({
  api: null as KeeperApi | null,
  domain: undefined as string | undefined,
}));

vi.mock('@/api/provider', () => ({ useOptionalKeeperApi: () => state.api }));
vi.mock('@/app/scope', () => ({
  useOptionalScope: () => (state.domain === undefined ? null : { domain: state.domain }),
}));

const document = (): SchemaDocument => ({
  kinds: {
    Supplier: {
      primary_key: ['code'],
      attributes: { code: { type: 'string' }, legal_name: { type: 'string' } },
    },
  },
  sources: {
    'approved.suppliers': {
      id: 'src_approved',
      type: 'parquet',
      connection: 'secret://env/APPROVED_PARQUET',
      target_kind: 'Supplier',
      key_mapping: ['code'],
      attribute_mapping: { legal_name: { column: 'name', transforms: [{ name: 'upper' }] } },
      sync_mode: 'snapshot',
    },
  },
});

const PREVIEW: FilePreview = {
  name: 'suppliers.parquet',
  columns: ['code', 'name'],
  rows: [
    { code: 'S1', name: 'acme' },
    { code: 'S2', name: 'bolt' },
  ],
  row_count: 2,
  truncated: false,
};

describe('Source preview of a Parquet file', () => {
  it('asks Keeper for the rows and maps them like any other sample', async () => {
    const preview = vi.fn().mockResolvedValue(PREVIEW);
    state.api = { inference: { preview } } as unknown as KeeperApi;
    state.domain = 'supply';
    const user = userEvent.setup();
    renderForm(SourcesForm, document());
    const file = new File(['PAR1'], 'suppliers.parquet');
    await user.upload(screen.getByLabelText('Sample file for approved.suppliers'), file);
    const table = await screen.findByRole('table', { name: 'Sample rows of approved.suppliers' });
    expect(preview).toHaveBeenCalledWith('supply', file, { rows: 5 });
    const rows = within(table).getAllByRole('row');
    expect(within(rows[1] as HTMLElement).getByText('ACME')).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByText('BOLT')).toBeInTheDocument();
  });

  it('shows what Keeper refused', async () => {
    const preview = vi
      .fn()
      .mockRejectedValue(new Error('suppliers.parquet exceeds 1048576 bytes.'));
    state.api = { inference: { preview } } as unknown as KeeperApi;
    state.domain = 'supply';
    const user = userEvent.setup();
    renderForm(SourcesForm, document());
    await user.upload(
      screen.getByLabelText('Sample file for approved.suppliers'),
      new File(['PAR1'], 'suppliers.parquet'),
    );
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('exceeds 1048576 bytes');
    });
  });

  it('says so when there is no Keeper to ask', async () => {
    state.api = null;
    state.domain = undefined;
    const user = userEvent.setup();
    renderForm(SourcesForm, document());
    await user.upload(
      screen.getByLabelText('Sample file for approved.suppliers'),
      new File(['PAR1'], 'suppliers.parquet'),
    );
    expect(await screen.findByRole('alert')).toHaveTextContent(
      messages.modelerEditors.source.preview.parquetNotRead,
    );
  });
});
