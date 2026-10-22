import { fireEvent, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { SchemaDocument } from './document';
import { moveStep } from './element-edits';
import { renderForm } from './form-harness';
import { OperationsForm } from './operation-form';
import { dumpSchemaYaml, parseSchemaYaml } from './yaml';

const steps = () => [
  { step: 'create_record', kind: 'Supplier', values: { code: '"S1"' }, x_note: 'first' },
  { step: 'create_record', kind: 'Contract', values: { code: '"C1"', status: '"draft"' } },
  {
    step: 'create_link',
    relation: 'with_supplier',
    from: '$step[1]',
    to: '$step[0]',
    x_note: 'link',
  },
];

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
    onboard: {
      id: 'opr_onboard',
      parameters: [],
      steps: steps(),
      review_policy: { mode: 'never' },
      x_owner: 'procurement',
    },
  },
});

const stepsOf = (doc: SchemaDocument) => doc.operations?.['onboard']?.['steps'];

describe('Operation step order', () => {
  it('moves a step with the buttons and renumbers the references to the steps', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(OperationsForm, document());
    await user.click(screen.getByRole('button', { name: 'Move step 1 down' }));
    const [supplier, contract, link] = steps();
    expect(stepsOf(current())).toEqual([
      contract,
      supplier,
      { ...link, from: '$step[0]', to: '$step[1]' },
    ]);
    expect(screen.getByRole('status', { name: 'Step order' })).toHaveTextContent(
      'Step 1 moved to position 2 of 3.',
    );
    // Unmodelled keys and the rest of the Operation are untouched.
    expect(current().operations?.['onboard']?.['x_owner']).toBe('procurement');
    expect(
      (stepsOf(current()) as Record<string, unknown>[]).find((s) => s['x_note'] === 'first'),
    ).toEqual(supplier);
  });

  it('moves back to the original order and keeps the focus on the pressed button', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(OperationsForm, document());
    await user.click(screen.getByRole('button', { name: 'Move step 3 up' }));
    expect(screen.getByRole('button', { name: 'Move step 2 up' })).toHaveFocus();
    await user.click(screen.getByRole('button', { name: 'Move step 2 down' }));
    expect(stepsOf(current())).toEqual(steps());
  });

  it('disables the moves that go out of the list', () => {
    renderForm(OperationsForm, document());
    expect(screen.getByRole('button', { name: 'Move step 1 up' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Move step 3 down' })).toBeDisabled();
  });

  it('reorders by dragging the handle of a step onto another', () => {
    const { current } = renderForm(OperationsForm, document());
    const [first] = screen.getAllByTitle('Drag to reorder');
    const target = screen.getByRole('listitem', { name: 'Step 3' });
    const dataTransfer = { setData: () => undefined, effectAllowed: '' };
    if (first === undefined) throw new Error('No drag handle.');
    fireEvent.dragStart(first, { dataTransfer });
    fireEvent.dragOver(target, { dataTransfer });
    fireEvent.drop(target, { dataTransfer });
    const [supplier, contract, link] = steps();
    expect(stepsOf(current())).toEqual([
      contract,
      { ...link, from: '$step[0]', to: '$step[2]' },
      supplier,
    ]);
  });

  it('round-trips a reordered Operation through YAML', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(OperationsForm, document());
    await user.click(screen.getByRole('button', { name: 'Move step 1 down' }));
    const parsed = parseSchemaYaml(dumpSchemaYaml(current()));
    expect(parsed.ok).toBe(true);
    if (parsed.ok) expect(parsed.document).toEqual(current());
  });
});

describe('moveStep', () => {
  it('leaves the Operation alone for a move that goes nowhere', () => {
    const operation = { steps: steps() };
    moveStep(operation, 1, 1);
    moveStep(operation, 0, 5);
    expect(operation).toEqual({ steps: steps() });
  });

  it('renumbers references in nested values and ignores references to missing steps', () => {
    const operation: Record<string, unknown> = {
      steps: [
        { step: 'create_record', kind: 'A', values: {} },
        {
          step: 'update_record',
          kind: 'A',
          target: '$step[0]',
          values: { n: 'f($step[0], $step[9])' },
        },
      ],
    };
    moveStep(operation, 0, 1);
    expect(operation['steps']).toEqual([
      {
        step: 'update_record',
        kind: 'A',
        target: '$step[1]',
        values: { n: 'f($step[1], $step[9])' },
      },
      { step: 'create_record', kind: 'A', values: {} },
    ]);
  });
});
