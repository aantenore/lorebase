import { EditorView } from '@codemirror/view';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderApp } from '@/test/render';
import { typeCel } from './form-harness';

const UNPROTECTED = '/supply/modeler?view=edit&branch=cleanup%2Ftiers';

async function openEditor(path = UNPROTECTED) {
  const user = userEvent.setup();
  const utils = renderApp(path);
  await screen.findByRole('textbox', { name: 'Schema YAML' });
  return { user, ...utils };
}

function yamlView(): EditorView {
  const dom = screen
    .getByRole('textbox', { name: 'Schema YAML' })
    .closest<HTMLElement>('.cm-editor');
  const view = dom === null ? null : EditorView.findFromDOM(dom);
  if (view === null) throw new Error('The YAML editor is not mounted.');
  return view;
}

const yamlText = () => yamlView().state.doc.toString();

/** Replace `from` with `to` in the YAML text, as typing in the editor would. */
function typeInYaml(from: string, to: string) {
  const view = yamlView();
  const index = view.state.doc.toString().indexOf(from);
  if (index < 0) throw new Error(`"${from}" is not in the YAML.`);
  view.dispatch({ changes: { from: index, to: index + from.length, insert: to } });
}

describe('Modeler schema editor: two synchronized views', () => {
  it('shows the Branch schema as a form and as YAML', async () => {
    await openEditor();
    expect(yamlText()).toContain('format: lorebase.schema/v1');
    expect(yamlText()).toContain('Supplier:');
    expect(screen.getByRole('combobox', { name: 'Kind' })).toHaveValue('Supplier');
    expect(screen.getByRole('textbox', { name: 'Name of city' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Type of tier' })).toHaveValue('enum');
    expect(screen.getByRole('textbox', { name: 'Enum values of tier' })).toHaveValue('1, 2, 3');
  });

  it('writes a form edit into the YAML', async () => {
    const { user } = await openEditor();
    await user.selectOptions(screen.getByRole('combobox', { name: 'Type of city' }), 'text');
    await waitFor(() => {
      expect(yamlText()).toMatch(/city:\n(?: {8}.+\n)*? {8}type: text/);
    });

    await user.click(screen.getByRole('checkbox', { name: 'Required: city' }));
    await waitFor(() => {
      expect(yamlText()).toMatch(/city:\n(?: {8}.+\n)*? {8}required: true/);
    });

    await user.click(screen.getByRole('button', { name: 'Add Attribute' }));
    expect(yamlText()).toContain('new_attribute:');
    expect(screen.getByRole('textbox', { name: 'Name of new_attribute' })).toBeInTheDocument();
  });

  it('writes a YAML edit into the form', async () => {
    await openEditor();
    typeInYaml('    risk_score:', '    risk_rating:');
    expect(await screen.findByRole('textbox', { name: 'Name of risk_rating' })).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'Name of risk_score' })).not.toBeInTheDocument();
  });

  it('keeps the stable id when a form rename rewrites the YAML', async () => {
    const { user } = await openEditor();
    const field = screen.getByRole('textbox', { name: 'Name of city' });
    await user.clear(field);
    await user.type(field, 'town');
    await waitFor(() => {
      expect(yamlText()).toMatch(/town:\n {8}id: att_supplier_city/);
    });
    expect(yamlText().match(/att_supplier_city/g)).toHaveLength(1);
  });

  it('follows the same document back and forth', async () => {
    const { user } = await openEditor();
    await user.selectOptions(screen.getByRole('combobox', { name: 'Type of city' }), 'text');
    await waitFor(() => {
      expect(yamlText()).toMatch(/city:\n(?: {8}.+\n)*? {8}type: text/);
    });
    typeInYaml('type: text', 'type: uri');
    await waitFor(() => {
      expect(screen.getByRole('combobox', { name: 'Type of city' })).toHaveValue('uri');
    });
  });

  it('adds a Kind in the form and shows it in the YAML', async () => {
    const { user } = await openEditor();
    await user.type(screen.getByRole('textbox', { name: 'New Kind name' }), 'Warehouse');
    await user.click(screen.getByRole('button', { name: 'Add Kind' }));
    expect(yamlText()).toContain('Warehouse:');
    expect(screen.getByRole('combobox', { name: 'Kind' })).toHaveValue('Warehouse');
  });

  it('edits an Operation in the form and writes it into the YAML', async () => {
    const { user } = await openEditor('/supply/modeler?view=edit&branch=model%2Frisk');
    await user.click(screen.getByRole('tab', { name: /^Operations/ }));
    expect(screen.getByRole('combobox', { name: 'Operation' })).toHaveValue('flag_supplier_risk');
    await typeCel('Precondition 1 expression', 'record(params.supplier).status == "active"');
    await waitFor(() => {
      expect(yamlText()).toContain('expression: record(params.supplier).status == "active"');
    });
    // The saved Operation can be dry run only once the change set is saved.
    expect(screen.getByText(/Save the change set first/)).toBeInTheDocument();
  });

  it("shows Keeper's expression type check next to the expression", async () => {
    const { user } = await openEditor('/supply/modeler?view=edit&branch=model%2Frisk');
    await user.click(screen.getByRole('tab', { name: /^Operations/ }));
    await typeCel('Precondition 1 expression', 'params.suppler != null');
    const field = await screen.findByRole('group', { name: 'Precondition 1 expression' });
    expect(
      await within(field).findByText("expression: undeclared reference to 'params.suppler'"),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save change set' })).toBeDisabled();
  });

  it('dry runs a saved Operation on the Branch', async () => {
    const { user } = await openEditor('/supply/modeler?view=edit&branch=model%2Frisk');
    await user.click(screen.getByRole('tab', { name: /^Operations/ }));
    await user.type(screen.getByRole('textbox', { name: 'Dry run value of supplier' }), 'S-1001');
    await user.click(screen.getByRole('button', { name: 'Dry run on model/risk' }));
    const result = await screen.findByText(/2 edits, 0 violations/);
    expect(result).toBeInTheDocument();
  });

  it('refuses a literal Source connection next to the field', async () => {
    const { user } = await openEditor();
    await user.click(screen.getByRole('tab', { name: /^Sources/ }));
    const connection = screen.getByRole('textbox', { name: 'Connection' });
    await user.clear(connection);
    await user.type(connection, 'postgres://erp@db/erp');
    expect(
      await screen.findAllByText('A connection is a secret:// reference, never a literal.'),
    ).not.toHaveLength(0);
    expect(screen.getByRole('button', { name: 'Save change set' })).toBeDisabled();
  });
});

describe('Modeler validation issues', () => {
  it('shows an issue with its path in the list and next to the field', async () => {
    await openEditor();
    typeInYaml('type: string', 'type: moneyish');
    const list = await screen.findByRole('region', { name: 'Validation issues' });
    expect(
      within(list).getByText(/^kinds\/Supplier\/attributes\/supplier_code\/type$/),
    ).toBeInTheDocument();
    // The same message sits under the Type select of the Attribute.
    expect(screen.getAllByText('Unknown type moneyish.')).toHaveLength(2);
  });

  it('moves the cursor to the line of an issue', async () => {
    const { user } = await openEditor();
    typeInYaml('type: string', 'type: moneyish');
    const jump = await screen.findByRole('button', {
      name: /^Go to line \d+: Unknown type moneyish\./,
    });
    await user.click(jump);
    const view = yamlView();
    const line = view.state.doc.lineAt(view.state.selection.main.head);
    expect(line.text).toContain('moneyish');
  });

  it('reports a name that breaks the naming rule and a broken reference', async () => {
    const { user } = await openEditor();
    const field = screen.getByRole('textbox', { name: 'Name of city' });
    await user.clear(field);
    await user.type(field, 'City');
    expect(
      (await screen.findAllByText('Attribute name City must be snake_case.')).length,
    ).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: 'Save change set' })).toBeDisabled();
  });

  it('locks the form while the YAML does not parse, and unlocks it when it does', async () => {
    await openEditor();
    typeInYaml('kinds:', 'kinds: [oops');
    expect(await screen.findByText(/The YAML has syntax errors/)).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Kind' })).toBeDisabled();
    expect(screen.getAllByText('yaml_syntax').length).toBeGreaterThan(0);

    typeInYaml('kinds: [oops', 'kinds:');
    await waitFor(() => {
      expect(screen.queryByText(/The YAML has syntax errors/)).not.toBeInTheDocument();
    });
    expect(screen.getByRole('combobox', { name: 'Kind' })).toBeEnabled();
  });
});

describe('Modeler change sets', () => {
  it('lists the pending commands and the migrations the Branch reports', async () => {
    const { user } = await openEditor();
    expect(screen.getByText('No pending schema changes on cleanup/tiers.')).toBeInTheDocument();

    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Type of risk_score' }),
      'decimal',
    );
    const summary = await screen.findByRole('region', { name: 'Change set' });
    expect(
      within(summary).getByText('update attribute risk_score in Supplier (type)'),
    ).toBeInTheDocument();
    const migrations = await within(summary).findByRole('table', { name: 'Data migrations' });
    expect(
      within(migrations).getByText('Cast Supplier.risk_score from integer to decimal'),
    ).toBeInTheDocument();
  });

  it('refuses a change set the Branch reports as invalid', async () => {
    const { user } = await openEditor();
    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Type of legal_name' }),
      'integer',
    );
    // In the issues list and, from the dry run, under the Type select of the Attribute.
    expect(
      await screen.findAllByText(/values of Supplier\.legal_name cannot be cast/),
    ).toHaveLength(2);
    expect(screen.getByRole('button', { name: 'Save change set' })).toBeDisabled();
  });

  it('saves a change set on the current Branch', async () => {
    const { user } = await openEditor();
    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Type of risk_score' }),
      'decimal',
    );
    await user.type(screen.getByRole('textbox', { name: 'Revision message' }), 'Widen risk_score');
    const save = screen.getByRole('button', { name: 'Save change set' });
    await waitFor(() => {
      expect(save).toBeEnabled();
    });
    await user.click(save);

    expect(
      await screen.findByText('Schema change set saved on cleanup/tiers.'),
    ).toBeInTheDocument();
    expect(
      await screen.findByText('No pending schema changes on cleanup/tiers.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Type of risk_score' })).toHaveValue('decimal');
  });

  it('sends a change set for a protected Branch through a new Branch and a Proposal', async () => {
    const { user, router } = await openEditor('/supply/modeler?view=edit');
    await user.type(screen.getByRole('textbox', { name: 'New Kind name' }), 'Warehouse');
    await user.click(screen.getByRole('button', { name: 'Add Kind' }));

    expect(screen.queryByRole('button', { name: 'Save change set' })).not.toBeInTheDocument();
    await user.type(
      screen.getByRole('textbox', { name: 'Revision message' }),
      'Add Warehouse Kind',
    );
    expect(screen.getByRole('textbox', { name: 'Branch for the change set' })).toHaveAttribute(
      'placeholder',
      'schema/add-warehouse-kind',
    );
    const open = screen.getByRole('button', { name: 'Open Proposal' });
    await waitFor(() => {
      expect(open).toBeEnabled();
    });
    await user.click(open);

    expect(
      await screen.findByRole('heading', { name: '#101 Add Warehouse Kind' }),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/supply/review/101');
    expect(await screen.findByText(/schema\/add-warehouse-kind into main/)).toBeInTheDocument();
  });

  it('puts the Branch schema back with Reset', async () => {
    const { user } = await openEditor();
    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Type of risk_score' }),
      'decimal',
    );
    await screen.findByRole('region', { name: 'Change set' });
    await user.click(screen.getByRole('button', { name: 'Reset to cleanup/tiers' }));
    expect(screen.getByRole('combobox', { name: 'Type of risk_score' })).toHaveValue('integer');
    expect(screen.getByText('No pending schema changes on cleanup/tiers.')).toBeInTheDocument();
  });
});
