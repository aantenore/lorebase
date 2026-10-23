import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { messages } from '@/messages';
import { renderApp } from '@/test/render';

const PATH = '/supply/review/44?tab=items';

async function openItems() {
  const user = userEvent.setup();
  const utils = renderApp(PATH);
  const table = await screen.findByRole('grid', { name: 'Extraction items' });
  return { user, table, ...utils };
}

const grid = () => screen.getByRole('grid', { name: 'Extraction items' });

const rowOf = (_table: HTMLElement, text: RegExp) => {
  const row = within(grid())
    .getAllByRole('row')
    .find((r) => text.test(r.textContent ?? ''));
  if (row === undefined) throw new Error(`No row matches ${text.source}`);
  return row;
};

/** The item rows now on screen (the grid is rebuilt when a filter empties it). */
const dataRows = (_table?: HTMLElement) =>
  within(grid())
    .getAllByRole('row')
    .filter((r) => r.hasAttribute('aria-selected'));

describe('Review: extraction items', () => {
  it('shows an Items tab for an extraction Proposal only', async () => {
    renderApp('/supply/review/44');
    expect(await screen.findByRole('tab', { name: 'Items' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true');
  });

  it('has no Items tab on a manual Proposal', async () => {
    renderApp('/supply/review/43');
    await screen.findByRole('heading', { name: /Tier cleanup/ });
    expect(screen.queryByRole('tab', { name: 'Items' })).not.toBeInTheDocument();
  });

  it('groups the items by type with confidence bars, match and evidence', async () => {
    const { table } = await openItems();
    const headers = within(grid())
      .getAllByRole('columnheader')
      .map((h) => h.textContent);
    expect(headers.filter((h) => /\(\d+\)$/.test(h ?? ''))).toEqual([
      'New Records (2)',
      'Attribute values (6)',
      'Links (5)',
    ]);
    const contract = rowOf(table, /Contract C-3025/);
    expect(within(contract).getByRole('meter', { name: 'Confidence' })).toHaveAttribute(
      'aria-valuenow',
      '0.425',
    );
    expect(within(contract).getByText('new')).toBeInTheDocument();
    expect(within(contract).getByText('Framework Supply Agreement C-3025')).toBeInTheDocument();
    expect(screen.getByText('11 pending')).toBeInTheDocument();
  });

  it('filters by status, Kind or Relation, type and confidence', async () => {
    const { user, table } = await openItems();
    expect(dataRows(table)).toHaveLength(13);
    const filters = screen.getByRole('form', { name: 'Filter items' });

    await user.selectOptions(within(filters).getByLabelText('Status'), 'accepted');
    expect(dataRows(table)).toHaveLength(1);
    await user.selectOptions(within(filters).getByLabelText('Status'), 'all');

    await user.selectOptions(within(filters).getByLabelText('Type'), 'link');
    expect(dataRows(table)).toHaveLength(5);
    await user.selectOptions(within(filters).getByLabelText('Kind or Relation'), 'feeds');
    expect(dataRows(table)).toHaveLength(2);
    await user.click(within(filters).getByRole('button', { name: 'Clear filters' }));

    await user.type(within(filters).getByLabelText('Confidence at least'), '0.6');
    await user.type(within(filters).getByLabelText('Confidence below'), '0.8');
    expect(dataRows(table)).toHaveLength(4);
    await user.clear(within(filters).getByLabelText('Confidence below'));
    await user.type(within(filters).getByLabelText('Confidence below'), '0.62');
    expect(dataRows(table)).toHaveLength(1);
  });

  it('jumps from the evidence of an item to its place in the document', async () => {
    const { user, table } = await openItems();
    await user.click(rowOf(table, /Contract C-3025/));
    const link = within(screen.getByRole('region', { name: /^Item Contract C-3025/ })).getByRole(
      'link',
      { name: 'Open in document' },
    );
    expect(link.getAttribute('href')).toContain('/supply/documents/doc_contract_c3025');
    expect(link.getAttribute('href')).toContain('item=pit_01');
    await user.click(link);
    const text = await screen.findByLabelText('Parsed text');
    const mark = await within(text).findByText('Framework Supply Agreement C-3025');
    expect(mark.tagName).toBe('MARK');
    expect(mark).toHaveAttribute('data-tone', 'active');
  });

  it('stages bulk decisions and applies them in one request, with the counts', async () => {
    const { user, table } = await openItems();
    expect(screen.getByText('Nothing staged.')).toBeInTheDocument();

    // The value items at or above 70%: S-1025 and S-1013 risk scores.
    await user.type(
      within(screen.getByRole('group', { name: 'Bulk decisions' })).getByLabelText('Threshold'),
      '{Backspace}{Backspace}{Backspace}0.7',
    );
    await user.click(screen.getByRole('button', { name: /^Accept 2 pending at or above 70%/ }));
    expect(screen.getByText('2 staged.')).toBeInTheDocument();
    expect(within(rowOf(table, /S-1025: risk_score/)).getByText('accepted')).toBeInTheDocument();
    expect(within(rowOf(table, /S-1025: risk_score/)).getByText('staged')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Apply decisions (2)' }));
    const outcome = await screen.findByRole('region', { name: 'Last applied decisions' });
    expect(outcome).toHaveTextContent('Applied 3 items: 3 changes on the Branch, 0 skipped');
    await waitFor(() => {
      expect(screen.getByText('3 accepted')).toBeInTheDocument();
    });
    expect(screen.getByText('Nothing staged.')).toBeInTheDocument();
  });

  it('shows what Keeper refuses: a Contract without its Link, then both together', async () => {
    const { user, table } = await openItems();
    await user.click(
      within(rowOf(table, /Contract C-3025/)).getByRole('button', { name: /^Accept Contract/ }),
    );
    await user.click(screen.getByRole('button', { name: 'Apply decisions (1)' }));
    const refusal = await screen.findByRole('alert', { name: messages.extraction.refusal.label });
    expect(refusal).toHaveTextContent('Contract/C-3025 needs a with_supplier Link');
    expect(refusal).toHaveTextContent('Nothing changed');
    // Nothing was applied: the staged decision is still there.
    expect(screen.getByRole('button', { name: 'Apply decisions (1)' })).toBeEnabled();

    await user.click(
      within(rowOf(table, /with_supplier: C-3025/)).getByRole('button', { name: /^Accept with/ }),
    );
    await user.click(screen.getByRole('button', { name: 'Apply decisions (2)' }));
    expect(await screen.findByRole('region', { name: 'Last applied decisions' })).toHaveTextContent(
      'Applied 3 items: 3 changes on the Branch',
    );
    expect(screen.queryByRole('alert', { name: messages.extraction.refusal.label })).toBeNull();
  });

  it('points an ambiguous match at another candidate before applying', async () => {
    const { user, table } = await openItems();
    await user.click(rowOf(table, /feeds: S-1013 to PL-14/));
    const detail = screen.getByRole('region', { name: /^Item feeds: S-1013 to PL-14/ });
    const picker = within(detail).getByRole('group', { name: 'Which Record is meant?' });
    expect(within(picker).getByRole('radio', { name: /Dresden Assembly/ })).toBeChecked();

    await user.click(within(picker).getByRole('radio', { name: /Dresden Warehouse/ }));
    expect(within(grid()).getByText('feeds: S-1013 to PL-15')).toBeInTheDocument();
    await user.click(within(detail).getByRole('button', { name: 'Accept' }));
    await user.click(screen.getByRole('button', { name: 'Apply decisions (1)' }));
    await screen.findByRole('region', { name: 'Last applied decisions' });
    expect(await within(grid()).findByText('feeds: S-1013 to PL-15')).toBeInTheDocument();
    expect(within(rowOf(table, /S-1013 to PL-15/)).getByText('accepted')).toBeInTheDocument();
  });

  it('edits the values of a new Record and checks them against the schema', async () => {
    const { user, table } = await openItems();
    await user.click(rowOf(table, /Contract C-3025/));
    const form = screen.getByRole('form', { name: messages.extraction.editor.label });
    const currency = within(form).getByLabelText('currency');
    await user.clear(currency);
    await user.type(currency, 'eur');
    expect(within(form).getAllByText(/Must match/).length).toBeGreaterThan(0);
    expect(within(form).getByRole('button', { name: 'Stage edits' })).toBeDisabled();

    await user.clear(currency);
    await user.type(currency, 'PLN');
    await user.click(within(form).getByRole('button', { name: 'Stage edits' }));
    expect(screen.getByText('1 staged.')).toBeInTheDocument();
    expect(
      within(screen.getByRole('form', { name: messages.extraction.editor.label })).getByLabelText(
        'currency',
      ),
    ).toHaveValue('PLN');
  });

  it('walks the table with the keyboard and stages decisions', async () => {
    const { user, table } = await openItems();
    table.focus();
    expect(dataRows(table)[0]).toHaveAttribute('aria-selected', 'true');
    await user.keyboard('j');
    expect(dataRows(table)[1]).toHaveAttribute('aria-selected', 'true');
    await user.keyboard('{ArrowDown}');
    expect(dataRows(table)[2]).toHaveAttribute('aria-selected', 'true');
    await user.keyboard('r');
    expect(within(dataRows(table)[2] as HTMLElement).getByText('rejected')).toBeInTheDocument();
    expect(screen.getByText('1 staged.')).toBeInTheDocument();
    await user.keyboard('u');
    expect(screen.getByText('Nothing staged.')).toBeInTheDocument();
    await user.keyboard('{End}');
    expect(dataRows(table).at(-1)).toHaveAttribute('aria-selected', 'true');
  });

  it('is read only once the Proposal is closed', async () => {
    const user = userEvent.setup();
    renderApp('/supply/review/44');
    await user.click(await screen.findByRole('button', { name: 'Close' }));
    await user.click(await screen.findByRole('tab', { name: 'Items' }));
    await screen.findByRole('grid', { name: 'Extraction items' });
    expect(screen.queryByRole('button', { name: /^Apply decisions/ })).toBeNull();
    expect(within(grid()).queryAllByRole('button')).toHaveLength(0);
  });
});
