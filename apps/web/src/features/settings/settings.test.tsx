import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { testConfig } from '@/config/env';
import { renderApp } from '@/test/render';

describe('Settings', () => {
  it('shows the runtime configuration and Domain settings', async () => {
    renderApp('/supply/settings', testConfig({ pageSize: 25 }));
    expect(await screen.findByRole('heading', { name: 'Settings', level: 1 })).toBeInTheDocument();

    const runtime = await screen.findByRole('table', { name: 'Runtime configuration' });
    expect(within(runtime).getByText('mock')).toBeInTheDocument();
    expect(within(runtime).getByText('25')).toBeInTheDocument();

    const domain = screen.getByRole('table', { name: 'Domain settings' });
    expect(within(domain).getByText('main')).toBeInTheDocument();
  });

  it('switches the theme and remembers it', async () => {
    const user = userEvent.setup();
    renderApp('/supply/settings');
    await screen.findByRole('heading', { name: 'Settings', level: 1 });
    const select = screen.getAllByLabelText('Theme')[1];
    if (select === undefined) throw new Error('Theme select not found');
    await user.selectOptions(select, 'dark');
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
    expect(window.localStorage.getItem('lorebase.theme')).toBe('dark');
    await user.selectOptions(select, 'system');
    expect(document.documentElement).not.toHaveAttribute('data-theme');
  });

  it('lists the Sources, History and Admin screens as planned', async () => {
    renderApp('/supply/settings');
    expect(await screen.findByRole('region', { name: 'Admin: Audit log' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'History: Log and compare' })).toBeInTheDocument();
  });
});
