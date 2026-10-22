import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderApp } from '@/test/render';

const PATH = '/supply/review?tab=suggestions';

const tableOf = () => screen.findByRole('table', { name: 'Schema suggestions' });

describe('Review: schema suggestions', () => {
  it('opens from the tab and keeps Proposals as the default', async () => {
    const user = userEvent.setup();
    renderApp('/supply/review');
    expect(await screen.findByRole('table', { name: 'Proposals' })).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: 'Schema suggestions' }));
    expect(await tableOf()).toBeInTheDocument();
  });

  it('lists pending suggestions above the minimum support', async () => {
    const user = userEvent.setup();
    renderApp(PATH);
    const table = await tableOf();
    expect(within(table).getByText('QualityNotice')).toBeInTheDocument();
    const ends = within(table).getByRole('row', { name: /Attribute ends/ });
    expect(within(ends).getByText('Supplier')).toBeInTheDocument();
    expect(within(ends).getByText('date (91%)')).toBeInTheDocument();
    const part = within(table).getByRole('row', { name: /Relation part/ });
    expect(within(part).getByText('QualityNotice (new) to Part')).toBeInTheDocument();
    expect(within(table).getByText('duplicate')).toBeInTheDocument();
    expect(within(table).getByText('withheld')).toBeInTheDocument();
    expect(within(table).queryByText('Memo')).not.toBeInTheDocument();

    await user.click(screen.getByLabelText('Include below min support'));
    expect(await within(await tableOf()).findByText('Memo')).toBeInTheDocument();
  });

  it('shows labels, samples and evidence with a link to the document', async () => {
    const user = userEvent.setup();
    renderApp(PATH);
    const table = await tableOf();
    await user.click(within(table).getByRole('button', { name: 'QualityNotice' }));
    const detail = await screen.findByRole('region', { name: 'Details of QualityNotice' });
    expect(within(detail).getByText('Quality notice, QN')).toBeInTheDocument();
    expect(within(detail).getByText('QN-2025-120')).toBeInTheDocument();
    expect(within(detail).getAllByText('Quality notice QN-2025-114').length).toBeGreaterThan(0);
    const link = within(detail).getAllByRole('link', { name: 'View in document' })[0];
    expect(link).toHaveAttribute('href', expect.stringContaining('/supply/documents/doc_quality'));
  });

  it('accepts a selection and links to the Proposal it opens', async () => {
    const user = userEvent.setup();
    renderApp(PATH);
    const table = await tableOf();
    await user.click(within(table).getByLabelText('Select QualityNotice'));
    await user.click(within(table).getByLabelText('Select part'));
    await user.click(screen.getByRole('button', { name: 'Accept selected' }));

    const outcome = await screen.findByRole('region', { name: 'Last decision' });
    const link = within(outcome).getByRole('link', { name: /Schema suggestions: QualityNotice/ });
    expect(link).toHaveAttribute('href', expect.stringMatching(/\/supply\/review\/\d+/));
    expect(within(outcome).getByText('create kinds.QualityNotice')).toBeInTheDocument();
    // Accepted suggestions leave the pending list.
    expect(within(await tableOf()).queryByText('QualityNotice')).not.toBeInTheDocument();
  });

  it('sends the rename of an accepted suggestion', async () => {
    const user = userEvent.setup();
    renderApp(PATH);
    const table = await tableOf();
    await user.click(within(table).getByRole('button', { name: 'QualityNotice' }));
    await user.type(await screen.findByLabelText('Accept QualityNotice as'), 'Notice');
    await user.click(within(table).getByLabelText('Select QualityNotice'));
    await user.click(screen.getByRole('button', { name: 'Accept selected' }));
    const outcome = await screen.findByRole('region', { name: 'Last decision' });
    expect(within(outcome).getByText('create kinds.Notice')).toBeInTheDocument();
  });

  it('shows the findings when the change set is invalid', async () => {
    const user = userEvent.setup();
    renderApp(PATH);
    const table = await tableOf();
    await user.click(within(table).getByLabelText('Select part'));
    await user.click(screen.getByRole('button', { name: 'Accept selected' }));
    const alert = await screen.findByRole('alert', { name: 'Keeper refused the decisions' });
    expect(alert).toHaveTextContent('which is not accepted in this request');
    expect(alert).toHaveTextContent('/decisions/0/suggestion');
    expect(within(await tableOf()).getByText('part')).toBeInTheDocument();
  });

  it('rejects a selection and lists it under the rejected filter', async () => {
    const user = userEvent.setup();
    renderApp(PATH);
    const table = await tableOf();
    await user.click(within(table).getByLabelText('Select ends'));
    await user.click(screen.getByRole('button', { name: 'Reject selected' }));
    expect(await screen.findByText('1 suggestion(s) decided.')).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText('Status'), 'rejected');
    expect(await within(await tableOf()).findByText('ends')).toBeInTheDocument();
  });
});
