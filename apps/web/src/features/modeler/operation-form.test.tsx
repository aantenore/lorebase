import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { SchemaDocument } from './document';
import { celView, renderForm, typeCel } from './form-harness';
import { OperationsForm } from './operation-form';

const document = (): SchemaDocument => ({
  kinds: {
    Contract: {
      primary_key: ['code'],
      attributes: { code: { type: 'string' }, status: { type: 'string' } },
    },
    Supplier: { primary_key: ['code'], attributes: { code: { type: 'string' } } },
  },
  relations: {
    with_supplier: {
      inverse_api_name: 'has_contracts',
      from_kind: 'Contract',
      to_kind: 'Supplier',
    },
  },
  operations: {
    approve_contract: {
      id: 'opr_approve',
      description: 'Approve a draft contract.',
      parameters: [{ name: 'contract', type: 'ref(Contract)', required: true, x_hint: 'kept' }],
      preconditions: [
        { expression: 'record(params.contract).status == "draft"', message: 'Draft only.' },
      ],
      steps: [
        {
          step: 'update_record',
          kind: 'Contract',
          target: 'params.contract',
          values: { status: '"active"' },
          x_note: 'kept',
        },
      ],
      review_policy: { mode: 'never' },
      permissions: ['contract-manager'],
      x_owner: 'procurement',
    },
  },
});

describe('Operation editor', () => {
  it('edits a precondition in place and keeps every key it does not edit', async () => {
    const { current } = renderForm(OperationsForm, document());
    await typeCel('Precondition 1 expression', 'record(params.contract).status == "open"');

    const operation = current().operations?.['approve_contract'];
    expect(operation?.['preconditions']).toEqual([
      { expression: 'record(params.contract).status == "open"', message: 'Draft only.' },
    ]);
    // Untouched fields, modelled or not, are exactly as they were.
    const before = document().operations?.['approve_contract'] ?? {};
    for (const key of ['id', 'description', 'parameters', 'steps', 'permissions', 'x_owner']) {
      expect(operation?.[key]).toEqual(before[key]);
    }
  });

  it('adds a typed parameter and edits step values as CEL', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(OperationsForm, document());
    await user.click(screen.getByRole('button', { name: 'Add parameter' }));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Parameter 2 type' }), 'enum');
    await user.type(screen.getByRole('textbox', { name: 'Parameter 2 enum values' }), 'low, high');
    await user.click(screen.getByRole('checkbox', { name: 'Parameter 2 required' }));
    expect(current().operations?.['approve_contract']?.['parameters']).toEqual([
      { name: 'contract', type: 'ref(Contract)', required: true, x_hint: 'kept' },
      { name: 'parameter_2', type: { enum: ['low', 'high'] }, required: true },
    ]);

    expect((await celView('Step 1 value of status')).state.doc.toString()).toBe('"active"');
    await typeCel('Step 1 target', '$step[0]');
    expect(current().operations?.['approve_contract']?.['steps']).toEqual([
      {
        step: 'update_record',
        kind: 'Contract',
        target: '$step[0]',
        values: { status: '"active"' },
        x_note: 'kept',
      },
    ]);
  });

  it('switches a step action, dropping only the fields the new action does not take', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(OperationsForm, document());
    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Step 1 action' }),
      'create_link',
    );
    expect(current().operations?.['approve_contract']?.['steps']).toEqual([
      {
        step: 'create_link',
        relation: 'with_supplier',
        from: '',
        to: '',
        values: { status: '"active"' },
        x_note: 'kept',
      },
    ]);
    expect(await screen.findByRole('textbox', { name: 'Step 1 from' })).toBeInTheDocument();
  });

  it('writes the review policy as a mode and an expression', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(OperationsForm, document());
    await user.selectOptions(screen.getByRole('combobox', { name: 'Review policy' }), 'expression');
    await typeCel('Review expression', 'record(params.contract).annual_value > 500000');
    expect(current().operations?.['approve_contract']?.['review_policy']).toEqual({
      mode: 'expression',
      expression: 'record(params.contract).annual_value > 500000',
    });
    await user.type(screen.getByRole('spinbutton', { name: 'Rate limit: calls' }), '60');
    await user.type(screen.getByRole('spinbutton', { name: 'Rate limit: per seconds' }), '60');
    expect(current().operations?.['approve_contract']?.['rate_limit']).toEqual({
      max_calls: 60,
      per_seconds: 60,
    });
  });

  it('shows the type check findings next to the expression they are about', async () => {
    renderForm(OperationsForm, document(), [
      {
        path: 'operations/approve_contract/preconditions/0/expression',
        code: 'schema.expression_invalid',
        message: "expression: undeclared reference to 'contrat'",
      },
      {
        path: 'operations/approve_contract/steps/0/values/status',
        code: 'schema.expression_invalid',
        message: 'expression: expected string, got int',
      },
    ]);
    const precondition = await celView('Precondition 1 expression');
    expect(precondition.contentDOM).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByText("expression: undeclared reference to 'contrat'")).toBeInTheDocument();
    expect(screen.getByText('expression: expected string, got int')).toBeInTheDocument();
  });

  it('adds and removes an Operation', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(OperationsForm, document());
    await user.type(screen.getByRole('textbox', { name: 'New Operation name' }), 'close_contract');
    await user.click(screen.getByRole('button', { name: 'Add Operation' }));
    await waitFor(() => {
      expect(screen.getByRole('combobox', { name: 'Operation' })).toHaveValue('close_contract');
    });
    expect(current().operations?.['close_contract']).toMatchObject({
      id: 'opr_operations_close_contract',
      steps: [{ step: 'create_record', kind: 'Contract', values: {} }],
      review_policy: { mode: 'never' },
    });
    await user.click(screen.getByRole('button', { name: 'Remove Operation' }));
    expect(Object.keys(current().operations ?? {})).toEqual(['approve_contract']);
  });
});
