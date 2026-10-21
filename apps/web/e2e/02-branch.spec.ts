import { expect, test } from '@playwright/test';
import { domain, keeper, screen, watchForErrors } from './helpers';

test('create a Branch from the top bar and switch to it', async ({ page }) => {
  const problems = watchForErrors(page);
  await page.goto(screen('explorer'));
  await expect(page.getByLabel('Branch')).toHaveValue('main');

  await page.getByRole('button', { name: 'New Branch' }).click();
  const dialog = page.getByRole('dialog', { name: 'New Branch' });
  await dialog.getByLabel('Branch name').fill('e2e/ui-branch');
  await dialog.getByRole('button', { name: 'Create Branch' }).click();

  // The dialog closes, the Branch selector follows and the URL carries it.
  await expect(dialog).toBeHidden();
  await expect(page.getByLabel('Branch')).toHaveValue('e2e/ui-branch');
  await expect(page).toHaveURL(/branch=e2e(%2F|\/)ui-branch/);
  await expect(page.getByText('Branch e2e/ui-branch created from main.')).toBeVisible();
  // It starts where main is, so it is neither ahead of nor behind it.
  await expect(page.getByText(/behind main/)).toHaveCount(0);

  // Keeper has it too, with the head of main.
  const created = await keeper().get<{ head: string; ahead: number }>(
    `/domains/${domain()}/branches/${encodeURIComponent('e2e/ui-branch')}`,
  );
  const main = await keeper().get<{ head: string }>(`/domains/${domain()}/branches/main`);
  expect(created.head).toBe(main.head);
  expect(created.ahead).toBe(0);
  expect(problems()).toEqual([]);
});

test('a Branch name Keeper refuses shows up as a toast, not as a crash', async ({ page }) => {
  await page.goto(screen('explorer'));
  await expect(page.getByLabel('Branch')).toHaveValue('main');
  await page.getByRole('button', { name: 'New Branch' }).click();
  const dialog = page.getByRole('dialog', { name: 'New Branch' });
  // `main` exists already.
  await dialog.getByLabel('Branch name').fill('main');
  await dialog.getByRole('button', { name: 'Create Branch' }).click();
  await expect(page.getByRole('alert')).toContainText(/exist|already/i);
  await expect(dialog).toBeVisible();
});
