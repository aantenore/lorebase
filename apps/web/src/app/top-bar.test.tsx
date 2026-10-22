import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { asRemote, createMockApi } from '@/api/mock/adapter';
import { testConfig } from '@/config/env';
import { urlOf } from '@/test/fetch';
import { renderApp } from '@/test/render';

const SESSION = {
  principal_id: 'p1',
  display_name: 'Ada Lovelace',
  csrf_header: 'X-CSRF-Token',
  csrf_token: 'tok',
  expires_at: '2030-01-01T00:00:00Z',
  idle_timeout_s: 1800,
};

describe('top bar in session mode', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows the signed-in name and signs out through Keeper', async () => {
    const assign = vi.fn();
    vi.stubGlobal('location', { ...window.location, pathname: '/supply/explorer', assign });
    const requests: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = urlOf(input);
        requests.push(`${init?.method ?? 'GET'} ${url}`);
        const body = url.endsWith('/auth/logout')
          ? { logout_url: 'https://idp.test/bye' }
          : SESSION;
        return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
      }),
    );
    const api = asRemote(createMockApi({ pageSize: 50, searchLimit: 20, maxNodes: 200 }), 0);
    const user = userEvent.setup();
    renderApp(
      '/supply/explorer',
      testConfig({ apiMode: 'live', authMode: 'session', keeperBaseUrl: 'https://keeper.test' }),
      api,
    );
    expect(await screen.findByText('Ada Lovelace')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Sign out' }));
    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith('https://idp.test/bye');
    });
    expect(requests).toContain('POST https://keeper.test/api/v1/auth/logout');
  });
});
