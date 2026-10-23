import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApiError } from '@/api/errors';
import { asRemote, createMockApi } from '@/api/mock/adapter';
import { TOKEN_STORAGE_KEY } from '@/api/credentials';
import { testConfig } from '@/config/env';
import { renderApp } from '@/test/render';

const sampleApi = () =>
  asRemote(createMockApi({ pageSize: 50, searchLimit: 20, maxNodes: 200 }), 0);
const liveConfig = (overrides = {}) =>
  testConfig({ apiMode: 'live', keeperBaseUrl: '', ...overrides });

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

  it('says in mock mode that no Keeper and token are used, and who the mock user is', async () => {
    renderApp('/supply/settings');
    const connection = await screen.findByRole('table', { name: 'Connection' });
    expect(within(connection).getByText('sample data (mock)')).toBeInTheDocument();
    expect(within(connection).getAllByText('not used in mock mode')).toHaveLength(2);
    expect(screen.queryByRole('form', { name: 'Store an API token' })).not.toBeInTheDocument();
    const caller = await screen.findByRole('table', { name: 'Caller' });
    expect(within(caller).getByText('Mock user')).toBeInTheDocument();
  });

  it('keeps a typed token in local storage, shows where it came from and forgets it', async () => {
    const user = userEvent.setup();
    renderApp('/supply/settings', liveConfig({ apiToken: 'lbk_env' }), sampleApi());
    const connection = await screen.findByRole('table', { name: 'Connection' });
    expect(within(connection).getByText('this origin (/api/v1)')).toBeInTheDocument();
    expect(within(connection).getByText('VITE_API_TOKEN')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Forget stored token' })).toBeDisabled();

    const form = screen.getByRole('form', { name: 'Store an API token' });
    expect(within(form).getByRole('button', { name: 'Save token' })).toBeDisabled();
    await user.type(within(form).getByLabelText('API token'), ' lbk_typed ');
    await user.click(within(form).getByRole('button', { name: 'Save token' }));

    expect(await screen.findByText('Token stored in this browser.')).toBeInTheDocument();
    expect(window.localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('lbk_typed');
    expect(within(connection).getByText('typed here (kept in this browser)')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Forget stored token' }));
    expect(window.localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(within(connection).getByText('VITE_API_TOKEN')).toBeInTheDocument();
  });

  it('shows a refused identity request as a hint, not a blank section', async () => {
    const api = sampleApi();
    const refusing = {
      ...api,
      auth: {
        whoami: () =>
          Promise.reject(
            new ApiError({
              type: 'about:blank',
              title: 'x',
              status: 401,
              detail: 'No credential.',
            }),
          ),
      },
    };
    renderApp('/supply/settings', liveConfig(), refusing);
    expect(await screen.findByText(/The server did not identify you/)).toHaveTextContent(
      'No credential. Set the API token in Settings.',
    );
  });
});
