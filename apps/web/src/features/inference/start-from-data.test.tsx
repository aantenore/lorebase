import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { testConfig, type AppConfig } from '@/config/env';
import { renderApp } from '@/test/render';

const csv = (name: string, body = 'a,b\n1,2\n') => new File([body], name, { type: 'text/csv' });

const HR = '/hr/modeler?view=start';

/** Opens the wizard and runs the inference over the named sample tables. */
async function infer(path: string, tables: string[], config: AppConfig = testConfig()) {
  const user = userEvent.setup();
  renderApp(path, config);
  const input = await screen.findByLabelText('Upload CSV or TSV files');
  await user.upload(
    input,
    tables.map((t) => csv(`${t}.csv`)),
  );
  await user.click(screen.getByRole('button', { name: 'Run inference' }));
  await screen.findByRole('heading', { name: 'Proposed schema' });
  return user;
}

/** Expands a Kind unless it is open already (the first Kind starts open). */
async function open(user: ReturnType<typeof userEvent.setup>, name: string) {
  const toggle = screen.queryByRole('button', { name: `Expand Kind ${name}` });
  if (toggle !== null) await user.click(toggle);
}

const kindCard = (name: string) => screen.getByRole('region', { name: `Kind ${name}` });
const yamlText = () => document.querySelector('.cm-content')?.textContent ?? '';
const progress = () => screen.getByRole('status', { name: 'Review progress' });
const pressed = (name: string) => screen.getByRole('button', { name });

describe('Start from data: reading the proposal', () => {
  it('lists Kinds with confidence, key evidence, sample values and reasons', async () => {
    const user = await infer(HR, ['supplier', 'purchase_order']);
    await open(user, 'Supplier');

    const supplier = kindCard('Supplier');
    expect(within(supplier).getAllByText(/Confidence/)[0]?.parentElement).toHaveTextContent('100%');
    expect(within(supplier).getByText('supplier_code')).toBeInTheDocument();
    await user.click(within(supplier).getByText('Key evidence'));
    expect(within(supplier).getByText(/named like a key \(supplier_code\)/)).toBeInTheDocument();

    const attributes = within(supplier).getByRole('table', { name: 'Attributes of Supplier' });
    const tier = within(attributes).getByLabelText('Name of Supplier.tier').closest('tr');
    if (tier === null) throw new Error('row not found');
    expect(within(tier).getByLabelText('Type of Supplier.tier')).toHaveValue('integer');
    expect(within(tier).getByText('1, 3, 2')).toBeInTheDocument();
    await user.click(within(tier).getByText('Why'));
    expect(within(tier).getByText('30 of 30 values are integer')).toBeInTheDocument();
  });

  it('lists Relations with cardinality and the inclusion ratio', async () => {
    await infer(HR, ['supplier', 'purchase_order']);
    const relations = screen.getByRole('table', { name: 'Proposed Relations' });
    const row = within(relations).getByLabelText('Name of Relation supplier').closest('tr');
    if (row === null) throw new Error('row not found');
    expect(within(row).getByText('PurchaseOrder to Supplier')).toBeInTheDocument();
    expect(within(row).getByText('many_to_one')).toBeInTheDocument();
    expect(within(row).getByText('100%', { selector: 'td' })).toBeInTheDocument();
  });

  it('shows the issues the inference reports', async () => {
    await infer(HR, ['supplier']);
    const issues = screen.getByRole('region', { name: 'Issues' });
    expect(within(issues).getByText('infer.ambiguous_primary_key')).toBeInTheDocument();
    expect(within(issues).getByText(/vat_number/)).toBeInTheDocument();
  });
});

describe('Start from data: editing the proposal', () => {
  it('renames, changes a type and drops an Attribute, and the YAML follows', async () => {
    const user = await infer(HR, ['supplier']);
    await open(user, 'Supplier');
    await user.click(screen.getByRole('button', { name: 'Accept Kind Supplier' }));
    await waitFor(() => {
      expect(yamlText()).toContain('legal_name');
    });

    const name = screen.getByLabelText('Name of Supplier.tier');
    await user.clear(name);
    await user.type(name, 'service_tier{Enter}');
    await waitFor(() => {
      expect(yamlText()).toContain('service_tier');
    });
    expect(yamlText()).not.toContain(' tier:');

    await user.selectOptions(screen.getByLabelText('Type of Supplier.service_tier'), 'string');
    await user.click(screen.getByRole('button', { name: 'Drop Supplier.city' }));
    await waitFor(() => {
      expect(yamlText()).not.toContain('city');
    });
    expect(yamlText()).toContain('service_tier');
    expect(progress()).toHaveTextContent('1 dropped');
  });

  it('renames a Kind and the Relations that point at it', async () => {
    const user = await infer(HR, ['supplier', 'purchase_order']);
    await user.click(screen.getByRole('button', { name: 'Accept all at or above 90%' }));
    const name = screen.getByLabelText('Name of Kind Supplier');
    await user.clear(name);
    await user.type(name, 'Vendor{Enter}');
    await waitFor(() => {
      expect(yamlText()).toContain('to_kind: Vendor');
    });
    expect(yamlText()).toContain('ref(Vendor)');
  });

  it('refuses a name that a sibling already uses', async () => {
    const user = await infer(HR, ['supplier', 'purchase_order']);
    const name = screen.getByLabelText('Name of Kind Supplier');
    await user.clear(name);
    await user.type(name, 'PurchaseOrder{Enter}');
    expect(await screen.findByText('PurchaseOrder is already used.')).toBeInTheDocument();
    expect(screen.getByLabelText('Name of Kind Supplier')).toHaveValue('Supplier');
  });

  it('does not let a primary key Attribute be dropped', async () => {
    const user = await infer(HR, ['supplier']);
    await open(user, 'Supplier');
    expect(screen.getByRole('button', { name: 'Drop Supplier.supplier_code' })).toBeDisabled();
  });
});

describe('Start from data: accepting by confidence', () => {
  it('accepts what reaches the threshold and leaves the rest pending', async () => {
    const user = await infer(HR, ['part', 'supplier', 'supplied_by']);
    expect(progress()).toHaveTextContent('0 accepted');

    await user.click(screen.getByRole('button', { name: 'Accept all at or above 90%' }));
    expect(pressed('Accept Kind Part')).toHaveAttribute('aria-pressed', 'true');
    expect(pressed('Accept Kind Supplier')).toHaveAttribute('aria-pressed', 'true');
    // supplied_by is a junction table read with 85% confidence.
    expect(pressed('Accept Relation supplied_by')).toHaveAttribute('aria-pressed', 'false');
    expect(progress()).toHaveTextContent('Applies 2 Kinds');
    expect(progress()).toHaveTextContent('0 Relations');
    expect(yamlText()).not.toContain('supplied_by');

    await user.clear(screen.getByLabelText('Confidence threshold (%)'));
    await user.type(screen.getByLabelText('Confidence threshold (%)'), '85');
    await user.click(screen.getByRole('button', { name: 'Accept all at or above 85%' }));
    expect(pressed('Accept Relation supplied_by')).toHaveAttribute('aria-pressed', 'true');
    expect(progress()).toHaveTextContent('1 Relations');
  });

  it('keeps what the reviewer dropped', async () => {
    const user = await infer(HR, ['supplier']);
    await user.click(screen.getByRole('button', { name: 'Drop Kind Supplier' }));
    await user.click(screen.getByRole('button', { name: 'Accept all at or above 90%' }));
    expect(pressed('Drop Kind Supplier')).toHaveAttribute('aria-pressed', 'true');
    expect(pressed('Accept Kind Supplier')).toHaveAttribute('aria-pressed', 'false');
  });

  it('leaves out a Relation whose Kind is not accepted and says why', async () => {
    const user = await infer(HR, ['supplier', 'purchase_order']);
    await user.click(screen.getByRole('button', { name: 'Accept Kind PurchaseOrder' }));
    await user.click(screen.getByRole('button', { name: 'Accept Relation supplier' }));
    // The Relation and the Attribute that references the Kind are both left out.
    expect(await screen.findAllByText('Left out: Supplier is not accepted')).toHaveLength(2);
  });
});

describe('Start from data: applying', () => {
  it('opens a Proposal from a new Branch when the Branch is protected', async () => {
    const user = await infer(HR, ['supplier', 'purchase_order']);
    expect(screen.getByText('No pending schema changes on main.')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Accept all at or above 90%' }));
    const change = screen.getByRole('region', { name: 'Change set' });
    await waitFor(() => {
      expect(within(change).getByText(/add kind Supplier/)).toBeInTheDocument();
    });
    expect(within(change).getByText(/main is protected/)).toBeInTheDocument();

    const apply = within(change).getByRole('button', { name: /^Apply as schema change/ });
    await waitFor(() => {
      expect(apply).toBeEnabled();
    });
    await user.click(apply);

    expect(
      await screen.findByRole('heading', { name: /Add the schema inferred from data/ }),
    ).toBeInTheDocument();
  });

  it('applies on an unprotected Branch as a schema Revision', async () => {
    const user = await infer('/supply/modeler?branch=cleanup%2Ftiers&view=start', ['risk_flag']);
    await user.click(screen.getByRole('button', { name: 'Accept Kind RiskFlag' }));
    const change = screen.getByRole('region', { name: 'Change set' });
    const apply = within(change).getByRole('button', { name: 'Apply as schema change' });
    await waitFor(() => {
      expect(apply).toBeEnabled();
    });
    await user.click(apply);
    expect(
      await screen.findByText(/Schema change set saved on cleanup\/tiers/),
    ).toBeInTheDocument();
  });

  it('merges into a Kind that exists, and the schema rules guard the merge', async () => {
    const user = await infer('/supply/modeler?view=start', ['plant']);
    expect(within(kindCard('Plant')).getByText(/on main: Attributes merge in/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Accept Kind Plant' }));

    // Plant implements the Located Trait, whose country is a string; the inferred one is an enum.
    const issues = screen.getByRole('region', { name: 'Issues' });
    expect(
      await within(issues).findByText(/country is enum but Located.country is string/),
    ).toBeInTheDocument();
    const change = screen.getByRole('region', { name: 'Change set' });
    expect(within(change).getByText('add attribute plant_type in Plant')).toBeInTheDocument();
    expect(within(change).queryByText(/add kind Plant/)).not.toBeInTheDocument();
    expect(within(change).getByRole('button', { name: /^Apply as schema change/ })).toBeDisabled();

    await user.selectOptions(screen.getByLabelText('Type of Plant.country'), 'string');
    await waitFor(() => {
      expect(within(issues).queryByText(/Located.country/)).not.toBeInTheDocument();
    });
    // The Branch checks the change against its Records and refuses what they break.
    expect(
      await within(issues).findByText(/3 Records violate Plant.plant_type: A value is required/),
    ).toBeInTheDocument();
    expect(within(change).getByText('main reports violations.')).toBeInTheDocument();
    expect(within(change).getByRole('button', { name: /^Apply as schema change/ })).toBeDisabled();
  });
});

describe('Start from data: choosing the data', () => {
  it('refuses files over the caps from the configuration and says which cap', async () => {
    const base = testConfig();
    const config = testConfig({
      inference: { ...base.inference, maxFileBytes: 10, maxFiles: 2 },
    });
    // A file picker would filter by suffix itself; a drop does not.
    const user = userEvent.setup({ applyAccept: false });
    renderApp(HR, config);
    const input = await screen.findByLabelText('Upload CSV or TSV files');
    await user.upload(input, [
      csv('big.csv', 'a,b,c,d\n1,2,3,4\n'),
      csv('notes.pdf', 'x'),
      csv('a.csv', 'x\n1\n'),
      csv('b.csv', 'x\n1\n'),
      csv('c.csv', 'x\n1\n'),
    ]);
    const alert = await screen.findByRole('alert', { name: 'Rejected files' });
    expect(within(alert).getByText(/big.csv: Larger than 10 B/)).toBeInTheDocument();
    expect(within(alert).getByText(/notes.pdf: Only .csv, .tsv, .txt/)).toBeInTheDocument();
    expect(within(alert).getByText(/c.csv: At most 2 files/)).toBeInTheDocument();
    const list = screen.getByRole('list', { name: 'Selected files' });
    expect(within(list).getAllByRole('listitem')).toHaveLength(2);
    expect(screen.getByText(/Up to 2 files, 10 B each/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Remove a.csv' }));
    expect(within(list).getAllByRole('listitem')).toHaveLength(1);
  });

  it('shows the error of the inference when the files are not tables it knows', async () => {
    const user = userEvent.setup();
    renderApp(HR);
    const input = await screen.findByLabelText('Upload CSV or TSV files');
    await user.upload(input, csv('vendors.csv'));
    await user.click(screen.getByRole('button', { name: 'Run inference' }));
    expect(await screen.findByText(/unknown: vendors/)).toBeInTheDocument();
  });

  it('infers from an allowlisted Postgres source and refuses a connection string', async () => {
    const user = userEvent.setup();
    renderApp(HR);
    await user.click(await screen.findByRole('tab', { name: 'Postgres source' }));
    const reference = screen.getByLabelText('Source reference');
    await user.type(reference, 'postgres://user:pw@db/erp');
    expect(screen.getByText(/connection strings are never sent/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Run inference' })).toBeDisabled();

    await user.clear(reference);
    await user.type(reference, 'secret://env/OTHER_DSN');
    await user.click(screen.getByRole('button', { name: 'Run inference' }));
    expect(await screen.findByText(/not in keeper.schema_inference/)).toBeInTheDocument();

    await user.clear(reference);
    await user.type(reference, 'secret://env/ERP_DSN');
    await user.type(screen.getByLabelText('Tables'), 'plant');
    await user.click(screen.getByRole('button', { name: 'Run inference' }));
    expect(await screen.findByRole('heading', { name: 'Proposed schema' })).toBeInTheDocument();
    expect(kindCard('Plant')).toBeInTheDocument();
  });

  it('goes back to the source step with Start over', async () => {
    const user = await infer(HR, ['plant']);
    await user.click(screen.getByRole('button', { name: 'Start over' }));
    expect(await screen.findByLabelText('Upload CSV or TSV files')).toBeInTheDocument();
  });
});
