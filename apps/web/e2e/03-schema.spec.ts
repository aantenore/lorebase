import { expect, test } from '@playwright/test';
import { domain, keeper, screen, watchForErrors } from './helpers';

interface SchemaBody {
  schema: {
    kinds: Record<string, { api_name: string; attributes: Record<string, { api_name: string }> }>;
  };
}

const supplierAttributes = async (branch: string): Promise<string[]> => {
  const body = await keeper().get<SchemaBody>(`/domains/${domain()}/schema`, { branch });
  const supplier = Object.values(body.schema.kinds).find((k) => k.api_name === 'Supplier');
  return Object.values(supplier?.attributes ?? {}).map((a) => a.api_name);
};

test.describe('schema change on a protected Branch', () => {
  test('an untouched schema produces no change set', async ({ page }) => {
    await page.goto(screen('modeler', 'main') + '&view=edit');
    await expect(page.getByRole('heading', { name: 'Modeler', level: 1 })).toBeVisible();
    // Keeper's document goes through the editor and back without a difference.
    await expect(page.getByText('No validation issues.')).toBeVisible();
    await expect(page.getByRole('heading', { name: /^Change set\s*0$/ })).toBeVisible();
    await expect(page.getByText('No pending schema changes.')).toBeVisible();
  });

  test('add an Attribute, open a Proposal, review it and merge it', async ({ page }) => {
    const problems = watchForErrors(page);
    expect(await supplierAttributes('main')).not.toContain('internal_notes');

    await page.goto(screen('modeler', 'main') + '&view=edit');
    await expect(
      page.getByText('This Branch is protected', { exact: false }).first(),
    ).toBeVisible();

    // Form: pick the Kind, add an Attribute and name it.
    await page.getByLabel('Kind', { exact: true }).selectOption('Supplier');
    await page.getByRole('button', { name: 'Add Attribute' }).click();
    await page.getByLabel('Name of new_attribute').fill('internal_notes');
    await expect(page.getByLabel('Name of internal_notes')).toBeVisible();

    // Keeper's dry run refuses an Attribute without a description, and the editor says so:
    // the Proposal cannot be opened yet.
    await expect(
      page.getByText(/internal_notes' of an active element needs a description/),
    ).toBeVisible();
    await expect(page.getByRole('button', { name: 'Open Proposal' })).toBeDisabled();

    await page.getByLabel('Description of internal_notes').fill('Free text notes of the buyers.');
    await expect(page.getByText(/needs a description/)).toBeHidden();

    // The change set says what the edit comes to, and Keeper finds nothing wrong with it.
    await expect(page.getByText('add attribute internal_notes in Supplier')).toBeVisible();
    await expect(page.getByText('No data migrations.')).toBeVisible();

    // Protected Branch: the change set goes to a new Branch and a Proposal.
    await page.getByLabel('Revision message').fill('Add internal_notes to Supplier');
    await expect(page.getByLabel('Branch for the change set')).toHaveAttribute(
      'placeholder',
      'schema/add-internal-notes-to-supplier',
    );
    await page.getByRole('button', { name: 'Open Proposal' }).click();

    // The Proposal page: nothing merges until the target Branch has its approval.
    await expect(page.getByText(/Proposal #prp_.* opened\./)).toBeVisible();
    await expect(
      page.getByRole('heading', { name: /Add internal_notes to Supplier/, level: 1 }),
    ).toBeVisible();
    await expect(page.getByText('0 of 1 approvals', { exact: false }).first()).toBeVisible();
    await expect(page.getByText('waiting')).toBeVisible();

    await page.getByRole('button', { name: 'Merge' }).click();
    await expect(page.getByRole('alert')).toContainText(/approval/i);

    await page.getByLabel('Comment').fill('Looks right.');
    await page.getByRole('button', { name: 'Submit review' }).click();
    await expect(page.getByText('Review added.')).toBeVisible();
    await expect(page.getByText('approved', { exact: true })).toBeVisible();

    await page.getByRole('button', { name: 'Merge' }).click();
    await expect(page.getByText(/Proposal #prp_.* merged\./)).toBeVisible();
    await expect(page.locator('main').getByText('merged', { exact: true }).first()).toBeVisible();

    // main has the Attribute, and the Modeler shows it.
    expect(await supplierAttributes('main')).toContain('internal_notes');
    await page.goto(screen('modeler', 'main'));
    await expect(page.getByRole('heading', { name: 'Modeler', level: 1 })).toBeVisible();
    await expect(page.getByText('Attributes (49)')).toBeVisible();
    // The only refusals are the ones the flow provokes: the dry run of the incomplete change set
    // (422) and the merge before the approval (409).
    expect(problems().filter((p) => !/^(409|422) /.test(p))).toEqual([]);
  });

  test('a Branch that does not exist is a page-level error, not a blank screen', async ({
    page,
  }) => {
    await page.goto(screen('modeler', 'no-such-branch'));
    await expect(page.locator('p[role=alert]')).toContainText("Branch 'no-such-branch' not found");
  });
});
