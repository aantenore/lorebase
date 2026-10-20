import { screen } from '@testing-library/react';
import { renderApp } from '@/test/render';

describe('router smoke', () => {
  it('renders the landing route', async () => {
    renderApp('/');
    expect(await screen.findByRole('heading', { name: 'Lorebase' })).toBeInTheDocument();
  });

  it('renders the not found view for unknown paths', async () => {
    renderApp('/a/b/c/d/e/nowhere');
    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument();
  });
});
