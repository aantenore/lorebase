import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { testConfig } from '@/config/env';
import { renderApp } from '@/test/render';

const sessionConfig = testConfig({
  apiMode: 'live',
  authMode: 'session',
  keeperBaseUrl: 'https://keeper.test',
});

describe('login route', () => {
  beforeEach(() => {
    vi.stubGlobal('location', { ...window.location, pathname: '/login', assign: vi.fn() });
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(new Response('{}', { status: 401 }))),
    );
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('explains a failure code', async () => {
    renderApp('/login?error=no_account', sessionConfig);
    expect(await screen.findByText(/no access. Ask an administrator/)).toBeInTheDocument();
  });

  it('starts the login at Keeper with the return path', async () => {
    const assign = vi.fn();
    vi.stubGlobal('location', { ...window.location, pathname: '/login', assign });
    const user = userEvent.setup();
    renderApp('/login?return_to=%2Fsupply%2Fcanvas', sessionConfig);
    await user.click(await screen.findByRole('button', { name: 'Sign in' }));
    expect(assign).toHaveBeenCalledWith(
      'https://keeper.test/api/v1/auth/login?return_to=%2Fsupply%2Fcanvas',
    );
  });

  it('says sign-in is not used outside session mode', async () => {
    renderApp('/login');
    expect(await screen.findByText(/does not use sign-in/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open Lorebase' })).toHaveAttribute('href', '/');
    expect(screen.queryByRole('button', { name: 'Sign in' })).not.toBeInTheDocument();
  });
});
