import { expect, test } from '@playwright/test';
import { countRevalidated, domain, keeper, scenario, screen, watchForErrors } from './helpers';

interface RecordBody {
  record: { values: Record<string, unknown> };
}

const supplier = async (key: string): Promise<Record<string, unknown>> =>
  (
    await keeper().get<RecordBody>(`/domains/${domain()}/kinds/Supplier/records/${key}`, {
      branch: 'main',
    })
  ).record.values;

test('inspect the Conflicts of a Proposal, resolve them and merge', async ({ page }) => {
  const problems = watchForErrors(page);
  const revalidated = countRevalidated(page);
  const { open } = scenario();

  // The setup merged `model/risk` first, so main has its values and `source/erp` collides.
  expect(await supplier('S-1001')).toMatchObject({ risk_score: '80' });
  expect(await supplier('S-1002')).toMatchObject({ tier: '1' });

  await page.goto(screen(`review/${open}`, 'main'));
  await expect(
    page.getByRole('heading', { name: /ERP sync: nightly supplier run/, level: 1 }),
  ).toBeVisible();

  // The list: two Conflicts of one kind, named by the Kind and Attribute they are about.
  await expect(page.getByRole('heading', { name: 'DV1 Attribute value (2)' })).toBeVisible();
  const conflicts = page.getByRole('listbox', { name: 'Conflicts' });
  await expect(conflicts.getByRole('option')).toHaveCount(2);
  await expect(conflicts.getByText('Supplier/S-1001 · risk_score')).toBeVisible();
  await expect(conflicts.getByText('Supplier/S-1002 · tier')).toBeVisible();
  await expect(page.getByText('0 of 2 resolved')).toBeVisible();

  // Base, ours and theirs for the risk score; the names in the message are not ids.
  await conflicts.getByRole('option', { name: /S-1001 · risk_score/ }).click();
  const resolver = page.getByRole('region', {
    name: /^Conflict DV1 Supplier\/S-1001 · risk_score/,
  });
  await expect(
    resolver.getByText('risk_score of S-1001 changed differently on both sides'),
  ).toBeVisible();
  const sides = resolver.getByRole('table');
  await expect(sides.getByRole('row').filter({ hasText: 'value' })).toHaveText(
    /32[\s\S]*80[\s\S]*45/,
  );

  // Custom value: an integer goes to Keeper as the canonical text "55".
  await resolver.getByRole('radio', { name: 'Custom value' }).check();
  await resolver.getByRole('spinbutton', { name: 'Custom value' }).fill('55');
  await resolver.getByRole('button', { name: 'Save resolution' }).click();
  await expect(page.getByText('Resolution saved.').first()).toBeVisible();
  await expect(conflicts.getByText('custom')).toBeVisible();

  // Tier: take theirs.
  await conflicts.getByRole('option', { name: /S-1002 · tier/ }).click();
  const tier = page.getByRole('region', { name: /^Conflict DV1 Supplier\/S-1002 · tier/ });
  await tier.getByRole('radio', { name: 'Take theirs' }).check();
  await tier.getByRole('button', { name: 'Save resolution' }).click();
  await expect(page.getByText('2 of 2 resolved')).toBeVisible();

  // The target is protected: it wants an approval before it takes the merge.
  await page.getByRole('button', { name: 'Apply resolutions' }).click();
  await expect(page.getByRole('alert')).toContainText(/approval/i);

  await page.getByRole('button', { name: 'Submit review' }).click();
  await expect(page.getByText('Review added.')).toBeVisible();
  await page.getByRole('button', { name: 'Apply resolutions' }).click();

  // Merged: the page shows the merge Revision, and main holds the resolved values.
  await expect(page.getByText(/Proposal #prp_.* merged\./)).toBeVisible();
  const merged = page.getByRole('region', { name: 'Merged Revision' });
  await expect(merged).toBeVisible();
  // Two parents (target and source), and both resolutions are listed with who made them.
  await expect(merged.getByText('(target)')).toBeVisible();
  await expect(merged.getByText('(source)')).toBeVisible();
  await expect(merged.getByRole('row', { name: /risk_score\s+custom/ })).toBeVisible();
  expect(await supplier('S-1001')).toMatchObject({ risk_score: '55' });
  expect(await supplier('S-1002')).toMatchObject({ tier: '2' });
  expect(problems().filter((p) => !/^409 /.test(p))).toEqual([]);
  // Every save refetched the Branch, the schema and the Proposal; the unchanged ones were 304s.
  expect(revalidated()).toBeGreaterThan(0);
});
