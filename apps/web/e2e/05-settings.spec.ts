import { expect, test } from '@playwright/test';
import { screen } from './helpers';

test('Settings shows the connection and who Keeper says the caller is', async ({ page }) => {
  await page.goto(screen('settings'));
  await expect(page.getByRole('heading', { name: 'Settings', level: 1 })).toBeVisible();

  const connection = page.getByRole('table', { name: 'Connection' });
  await expect(
    connection.getByRole('row', { name: /Data source\s+Keeper over REST/ }),
  ).toBeVisible();
  await expect(connection.getByRole('row', { name: /Keeper\s+this origin/ })).toBeVisible();

  // The stack runs with authentication off: Keeper calls every caller the same administrator.
  const caller = page.getByRole('table', { name: 'Caller' });
  await expect(caller.getByRole('row', { name: /Id\s+anonymous/ })).toBeVisible();
  await expect(caller.getByRole('row', { name: /Installation administrator\s+yes/ })).toBeVisible();
});

test('a stored API token is sent as a Bearer header and can be forgotten', async ({ page }) => {
  const seen: string[] = [];
  page.on('request', (request) => {
    const header = request.headers()['authorization'];
    if (request.url().includes('/api/v1/') && header !== undefined) seen.push(header);
  });
  await page.goto(screen('settings'));
  await expect(page.getByRole('table', { name: 'Caller' })).toBeVisible();
  expect(seen).toEqual([]);

  await page.getByLabel('API token', { exact: true }).fill('lbk_e2e_token');
  await page.getByRole('button', { name: 'Save token' }).click();
  await expect(page.getByText('Token stored in this browser.')).toBeVisible();
  await expect(
    page
      .getByRole('table', { name: 'Connection' })
      .getByRole('row', { name: /API token\s+typed here/ }),
  ).toBeVisible();
  await expect.poll(() => seen.at(-1)).toBe('Bearer lbk_e2e_token');

  await page.getByRole('button', { name: 'Forget stored token' }).click();
  await expect(
    page.getByRole('table', { name: 'Connection' }).getByRole('row', { name: /API token\s+none/ }),
  ).toBeVisible();
  expect(await page.evaluate(() => window.localStorage.getItem('lorebase.apiToken'))).toBeNull();
});
