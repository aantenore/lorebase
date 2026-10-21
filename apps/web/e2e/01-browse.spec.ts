import { expect, test } from '@playwright/test';
import { screen, watchForErrors } from './helpers';

test.describe('Explorer and Canvas against Keeper', () => {
  test('browse Supplier S-1001: table, Record page with links and lineage', async ({ page }) => {
    const problems = watchForErrors(page);
    await page.goto(screen('explorer'));
    await expect(page.getByRole('heading', { name: 'Explorer', level: 1 })).toBeVisible();

    // The Domain home lists the Kinds of the schema Keeper serves.
    await page.getByRole('link', { name: 'Supplier', exact: true }).click();
    await expect(page.getByRole('table', { name: 'Supplier Records' })).toBeVisible();
    await expect(page.getByRole('row', { name: /S-1001/ })).toBeVisible();

    await page.getByRole('link', { name: 'Kavsen Alloys Sp. z o.o.' }).click();
    await expect(
      page.getByRole('heading', { name: 'Kavsen Alloys Sp. z o.o.', level: 1 }),
    ).toBeVisible();

    const attributes = page.getByRole('table', { name: 'Attributes' });
    await expect(attributes.getByRole('row', { name: /Country\s+PL/ })).toBeVisible();
    await expect(attributes.getByRole('row', { name: /Supplier code\s+S-1001/ })).toBeVisible();
    // Keeper writes integers as strings; the screen shows them as numbers all the same.
    await expect(attributes.getByRole('row', { name: /Risk score\s+80\s+integer/ })).toBeVisible();

    // Links: S-1001 supplies at least one Part.
    const supplies = page.getByRole('heading', { name: /^supplies/ });
    await expect(supplies).toBeVisible();
    await expect(
      page.getByRole('link', { name: /seal|bearing|filter|valve/i }).first(),
    ).toBeVisible();

    // Lineage: the sample came in by import, the risk score by the merged Operation.
    const lineage = page.getByRole('table', { name: 'Lineage of each value' });
    await expect(lineage.getByRole('row', { name: /supplier_code\s+import/ })).toBeVisible();
    await expect(lineage.getByRole('row', { name: /risk_score\s+operation/ })).toBeVisible();

    expect(problems()).toEqual([]);
  });

  test('open the Record in the Canvas: the graph comes from the neighborhood endpoint', async ({
    page,
  }) => {
    const problems = watchForErrors(page);
    await page.goto(screen('explorer/Supplier/S-1001'));
    await page.getByRole('link', { name: 'Open in Canvas' }).click();

    await expect(page.getByRole('heading', { name: 'Canvas', level: 1 })).toBeVisible();
    await expect(page).toHaveURL(/seed=Supplier(%2F|\/)S-1001/);
    await expect(page.locator('[role=img][aria-label^="Records graph"]')).toBeVisible();

    const list = page.getByText(/^List view: \d+ nodes, \d+ edges$/);
    await expect(list).toBeVisible();
    await list.click();
    // The seed and the Parts it supplies are in the graph; Keeper reports each Link in its
    // stored direction, so the edge reads Part supplied_by Supplier.
    await expect(
      page.getByText(/^Part\/P-\d+ supplied_by Supplier\/S-1001$/).first(),
    ).toBeVisible();

    // The Schema graph is drawn from the schema of the same Branch.
    await page.getByRole('tab', { name: 'Schema graph' }).click();
    await expect(page.locator('[role=img][aria-label^="Schema graph"]')).toBeVisible();
    expect(problems()).toEqual([]);
  });

  test('search finds a Supplier by a word of its name', async ({ page }) => {
    await page.goto(screen('explorer'));
    await expect(page.getByRole('heading', { name: 'Explorer', level: 1 })).toBeVisible();
    await page.keyboard.press('Control+k');
    const dialog = page.getByRole('dialog', { name: 'Search Records' });
    await dialog.getByLabel('Search query').fill('Kavsen');
    await page.keyboard.press('Enter');
    await expect(page).toHaveURL(/q=Kavsen/);
    await expect(page.getByRole('link', { name: /Kavsen Alloys/ }).first()).toBeVisible();
  });
});
