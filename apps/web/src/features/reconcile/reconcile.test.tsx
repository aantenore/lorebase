import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderApp } from '@/test/render';

const PATH = '/supply/reconcile';

const detailOf = (title: string) => screen.findByRole('region', { name: `Candidate ${title}` });

describe('Reconciliation', () => {
  it('opens from the navigation with a tab per type and its count', async () => {
    const user = userEvent.setup();
    renderApp('/supply/review');
    await user.click(await screen.findByRole('link', { name: 'Reconciliation' }));
    expect(await screen.findByRole('tab', { name: 'Contradictions (5)' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(await screen.findByRole('tab', { name: 'Duplicates (3)' })).toBeInTheDocument();
    expect(await screen.findByRole('tab', { name: 'Stale (2)' })).toBeInTheDocument();
    expect(await screen.findByRole('tab', { name: 'Superseded (2)' })).toBeInTheDocument();

    // A negation ranks first, an equivalent last.
    const list = await screen.findByRole('list', { name: 'Candidates' });
    const rows = within(list).getAllByRole('listitem');
    expect(rows[0]).toHaveTextContent('Plant GLW · logistics_note');
    expect(rows[0]).toHaveTextContent('negation');
    expect(rows[rows.length - 1]).toHaveTextContent('Plant MNZ · logistics_note');
    expect(rows[rows.length - 1]).toHaveTextContent('equivalent');
  });

  it('filters by Kind and status', async () => {
    const user = userEvent.setup();
    renderApp(PATH);
    await screen.findByRole('list', { name: 'Candidates' });
    await user.selectOptions(screen.getByLabelText('Kind'), 'Supplier');
    await user.selectOptions(screen.getByLabelText('Status'), 'resolved');
    const list = await screen.findByRole('list', { name: 'Candidates' });
    expect(await within(list).findByText('Supplier S-1019 · risk_score')).toBeInTheDocument();
    expect(within(list).getAllByRole('listitem')).toHaveLength(1);
  });

  it('shows every claim with source, time, period, evidence and signals', async () => {
    const user = userEvent.setup();
    renderApp(`${PATH}?candidate=rcn_risk_s1025`);
    const detail = await detailOf('Supplier S-1025 · risk_score');
    const first = await within(detail).findByRole('article', { name: 'Claim 1' });
    expect(await within(first).findByText('current value')).toBeInTheDocument();
    expect(within(first).getByText('Source erp.vendors')).toBeInTheDocument();
    const second = within(detail).getByRole('article', { name: 'Claim 2' });
    expect(within(second).getByText('Document doc_supplier_review_q4')).toBeInTheDocument();
    expect(within(second).getByText('2026-10-20 08:01 UTC')).toBeInTheDocument();
    expect(within(second).getByRole('link', { name: 'View in document' })).toHaveAttribute(
      'href',
      expect.stringContaining('/supply/documents/doc_supplier_review_q4'),
    );
    expect(within(second).getByRole('link', { name: 'Item of Proposal #44' })).toBeInTheDocument();

    await user.click(screen.getByRole('tab', { name: /Superseded/ }));
    await user.click(await screen.findByRole('button', { name: /Contract C-2232/ }));
    const terms = await detailOf('Contract C-2232 · payment_terms_days');
    expect(within(terms).getByText('2025-01-01 to open')).toBeInTheDocument();
    expect(within(terms).getByText('periods')).toBeInTheDocument();
  });

  it('marks what a text claim changes in the current value', async () => {
    renderApp(`${PATH}?candidate=rcn_note_trn`);
    const detail = await detailOf('Plant TRN · logistics_note');
    const second = await within(detail).findByRole('article', { name: 'Claim 2' });
    expect(await within(second).findByText('added:')).toBeInTheDocument();
    expect(within(detail).getByText('unit')).toBeInTheDocument();
  });

  it('needs a reason, previews a policy and proposes a claim on a protected Branch', async () => {
    const user = userEvent.setup();
    renderApp(`${PATH}?candidate=rcn_risk_s1025`);
    const detail = await detailOf('Supplier S-1025 · risk_score');
    const form = await within(detail).findByRole('form', { name: 'Decision' });

    await user.click(within(form).getByRole('button', { name: 'Apply decision' }));
    expect(await within(form).findByText('Enter a reason.')).toBeInTheDocument();

    await user.click(within(form).getByLabelText('Apply a policy'));
    await user.type(within(form).getByLabelText('Reason'), 'The ERP owns the score.');
    await user.click(within(form).getByRole('button', { name: 'Preview' }));
    const preview = await within(detail).findByRole('status', { name: 'Preview' });
    expect(preview).toHaveTextContent('Picks claim 1.');
    expect(preview).toHaveTextContent('The current value stays');

    await user.click(within(form).getByLabelText('Take a claim'));
    await user.click(within(form).getByRole('button', { name: 'Apply decision' }));
    const [link] = await within(detail).findAllByRole('link', { name: /Open Proposal #\d+/ });
    expect(link).toHaveAttribute('href', expect.stringMatching(/\/supply\/review\/\d+/));
  });

  it('dismisses with a reason and reopens', async () => {
    const user = userEvent.setup();
    renderApp(`${PATH}?candidate=rcn_note_mnz`);
    const detail = await detailOf('Plant MNZ · logistics_note');
    const form = await within(detail).findByRole('form', { name: 'Decision' });
    await user.type(within(form).getByLabelText('Reason'), 'Same rule, other words.');
    await user.click(within(form).getByRole('button', { name: 'Dismiss' }));
    expect(await within(detail).findByText('Candidate dismissed.')).toBeInTheDocument();

    const reopen = await within(detail).findByRole('form', { name: 'Reopen' });
    await user.type(within(reopen).getByLabelText('Reason'), 'The site changed.');
    await user.click(within(reopen).getByRole('button', { name: 'Reopen' }));
    expect(await within(detail).findByText('Candidate reopened.')).toBeInTheDocument();
  });

  it('merges duplicates through a Proposal with a value per Attribute', async () => {
    const user = userEvent.setup();
    renderApp(`${PATH}?type=duplicate&candidate=rcn_dup_pl14_pl15`);
    const detail = await detailOf('Plant PL-14 / PL-15');
    const table = await within(detail).findByRole('table', { name: 'Values that differ' });
    await user.click(within(table).getByLabelText('name: take the value of PL-15'));
    await user.type(within(detail).getByLabelText('Reason'), 'One site.');
    await user.click(within(detail).getByRole('button', { name: 'Merge through a Proposal' }));
    // In the outcome and in the header, which links the candidate to its Proposal.
    expect(await within(detail).findAllByRole('link', { name: /Open Proposal #\d+/ })).toHaveLength(
      2,
    );
  });

  it('dismisses a selection in bulk', async () => {
    const user = userEvent.setup();
    renderApp(PATH);
    const list = await screen.findByRole('list', { name: 'Candidates' });
    await user.click(within(list).getByLabelText('Select Plant TRN · logistics_note'));
    await user.click(within(list).getByLabelText('Select Plant GLW · logistics_note'));
    const bulk = screen.getByRole('form', { name: 'Bulk decision' });
    await user.selectOptions(within(bulk).getByLabelText('Action'), 'dismiss');
    await user.type(within(bulk).getByLabelText('Reason'), 'Survey superseded.');
    await user.click(within(bulk).getByRole('button', { name: 'Apply' }));
    expect(await within(bulk).findByText('2 decided: 2 dismissed.')).toBeInTheDocument();
    expect(await screen.findByRole('tab', { name: 'Contradictions (3)' })).toBeInTheDocument();
  });

  it('marks the values of a Record that have open candidates', async () => {
    renderApp('/supply/explorer/Supplier/S-1025');
    const link = await screen.findByRole('link', { name: '1 open candidate' });
    expect(link).toHaveAttribute('href', expect.stringContaining('candidate=rcn_risk_s1025'));
    expect(
      await screen.findByRole('link', { name: 'Possible duplicate: S-1013' }),
    ).toBeInTheDocument();
  });

  it('lists the open candidates in the reconciliation check of a Proposal', async () => {
    renderApp('/supply/review/44');
    const candidates = await screen.findByRole('list', { name: 'Open candidates' });
    expect(within(candidates).getByRole('link', { name: 'rcn_risk_s1025' })).toHaveAttribute(
      'href',
      expect.stringContaining('/supply/reconcile?candidate=rcn_risk_s1025'),
    );
  });
});
