import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { makeConflict } from '@/api/mock/conflicts';
import type { Conflict, Resolution } from '@/api/types';
import type { ValueSpec } from '@/api/values';
import { ConflictResolver } from './conflict-resolver';
import { diffRows } from './side-by-side';

const names = new Map<string, string>([['knd_supplier', 'Supplier']]);

const tierSpec: ValueSpec = { type: 'enum', constraints: { enum_values: ['1', '2', '3'] } };

const tier = (): Conflict =>
  makeConflict({
    kind: 'DV1',
    partition: 'Supplier',
    key: 'S-1003',
    attribute: 'att_supplier_tier',
    base: '2',
    ours: '3',
    theirs: '1',
    message: 'The tier changed on both Branches.',
  });

const members = (): Conflict =>
  makeConflict({
    kind: 'XS4',
    partition: 'Supplier',
    attribute: 'att_supplier_risk_band',
    sample: ['S-1004', 'S-1005', 'S-1006'],
    message: '3 Suppliers have no risk_band.',
    details: {
      members: {
        'S-1004': { base: null, ours: null, theirs: null },
      },
    },
  });

function setup(
  conflict: Conflict,
  props: {
    spec?: ValueSpec | null;
    resolution?: Resolution;
    readOnly?: boolean;
  } = {},
) {
  const onSave = vi.fn();
  render(
    <ConflictResolver
      conflict={conflict}
      resolution={props.resolution}
      names={names}
      spec={props.spec === undefined ? null : props.spec}
      readOnly={props.readOnly === true}
      onSave={onSave}
    />,
  );
  return { onSave, user: userEvent.setup() };
}

describe('ConflictResolver choices', () => {
  it('shows base, ours and theirs side by side with the sides that moved marked', () => {
    setup(tier(), { spec: tierSpec });
    const table = screen.getByRole('table', { name: 'Base, ours and theirs' });
    const row = within(table).getByRole('row', { name: /^value/ });
    expect(row.querySelector('[data-side="base"]')).toHaveTextContent('2');
    const ours = row.querySelector('[data-side="ours"]');
    const theirs = row.querySelector('[data-side="theirs"]');
    expect(ours).toHaveTextContent('3');
    expect(ours).toHaveAttribute('data-changed', 'true');
    expect(theirs).toHaveTextContent('1');
    expect(theirs).toHaveAttribute('data-changed', 'true');
  });

  it('saves a picked side without a value', async () => {
    const conflict = tier();
    const { onSave, user } = setup(conflict, { spec: tierSpec });
    expect(screen.getByRole('button', { name: 'Save resolution' })).toBeDisabled();

    await user.click(screen.getByRole('radio', { name: 'Take theirs' }));
    await user.click(screen.getByRole('button', { name: 'Save resolution' }));

    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave).toHaveBeenCalledWith({ fingerprint: conflict.fingerprint, choice: 'theirs' });
  });

  it('offers exactly the choices of the Conflict kind', () => {
    setup(tier(), { spec: tierSpec });
    const radios = screen.getAllByRole('radio').map((r) => r.closest('label')?.textContent);
    expect(radios).toEqual(['Take ours', 'Take theirs', 'Revert to base', 'Custom value']);
  });

  it('offers only drop and restore for a dangling Link', () => {
    setup(makeConflict({ kind: 'XS9', partition: 'feeds', sample: ['a -> b'], message: 'm' }));
    expect(screen.getAllByRole('radio').map((r) => r.closest('label')?.textContent)).toEqual([
      'Drop the Links',
      'Restore the Record',
    ]);
  });

  it('edits a custom value with the input of its type and saves it', async () => {
    const conflict = tier();
    const { onSave, user } = setup(conflict, { spec: tierSpec });
    await user.click(screen.getByRole('radio', { name: 'Custom value' }));
    await user.selectOptions(screen.getByLabelText('Custom value', { selector: 'select' }), '2');
    await user.click(screen.getByRole('button', { name: 'Save resolution' }));
    expect(onSave).toHaveBeenCalledWith({
      fingerprint: conflict.fingerprint,
      choice: 'custom',
      value: '2',
    });
  });

  it('blocks a custom value that breaks the constraints of the Attribute', async () => {
    const score = makeConflict({
      kind: 'DV1',
      partition: 'Supplier',
      key: 'S-1003',
      attribute: 'att_supplier_risk_score',
      base: 50,
      ours: 71,
      theirs: 40,
      message: 'm',
    });
    const spec: ValueSpec = { type: 'integer', constraints: { min: 0, max: 60 } };
    const { onSave, user } = setup(score, { spec });
    await user.click(screen.getByRole('radio', { name: 'Custom value' }));
    const field = screen.getByRole('spinbutton', { name: 'Custom value' });
    await user.clear(field);
    await user.type(field, '75');

    expect(screen.getByText('Must be at most 60.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save resolution' })).toBeDisabled();

    await user.clear(field);
    await user.type(field, '55');
    await user.click(screen.getByRole('button', { name: 'Save resolution' }));
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ choice: 'custom', value: 55 }));
  });

  it('keeps JSON text that does not parse from being saved', async () => {
    const record = makeConflict({
      kind: 'DV2',
      partition: 'Plant',
      key: 'MNZ',
      base: { name: 'a' },
      ours: { name: 'b' },
      theirs: null,
      message: 'm',
    });
    const { user } = setup(record, { spec: { type: 'json' } });
    await user.click(screen.getByRole('radio', { name: 'Custom value' }));
    const editor = screen.getByRole('textbox', { name: 'Custom value' });
    await user.clear(editor);
    await user.type(editor, '{{"name": ');
    expect(screen.getByText('Not valid JSON.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save resolution' })).toBeDisabled();
  });

  it('shows the stored Resolution and lets it be updated', () => {
    const conflict = tier();
    setup(conflict, {
      spec: tierSpec,
      resolution: { fingerprint: conflict.fingerprint, choice: 'ours' },
    });
    expect(screen.getByText('resolved: ours')).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: 'Take ours' })).toBeChecked();
    expect(screen.getByRole('button', { name: 'Update resolution' })).toBeInTheDocument();
  });

  it('explains why the last merge run refused a stored Resolution', () => {
    const conflict = {
      ...tier(),
      details: { rejected: 'custom refused: Must be one of: 1, 2, 3.' },
    };
    setup(conflict, { spec: tierSpec });
    expect(screen.getByRole('alert')).toHaveTextContent('Must be one of: 1, 2, 3.');
  });

  it('is read only once the Proposal is closed', () => {
    setup(tier(), { spec: tierSpec, readOnly: true });
    expect(screen.getByRole('radio', { name: 'Take ours' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Save resolution' })).toBeDisabled();
  });
});

describe('ConflictResolver grouped Conflicts', () => {
  const bandSpec: ValueSpec = {
    type: 'enum',
    required: true,
    constraints: { enum_values: ['low', 'medium', 'high'] },
  };

  it('asks for a value per member and saves them keyed by member', async () => {
    const conflict = members();
    const { onSave, user } = setup(conflict, { spec: bandSpec });
    expect(screen.getByText('3 members')).toBeInTheDocument();

    await user.click(screen.getByRole('radio', { name: 'Custom value' }));
    const table = screen.getByRole('table', { name: 'Values per member' });
    expect(within(table).getAllByRole('row')).toHaveLength(4);
    expect(screen.getByRole('button', { name: 'Save resolution' })).toBeDisabled();

    await user.selectOptions(screen.getByLabelText('Value for S-1004'), 'low');
    await user.selectOptions(screen.getByLabelText('Value for S-1005'), 'medium');
    expect(screen.getByRole('button', { name: 'Save resolution' })).toBeDisabled();
    await user.selectOptions(screen.getByLabelText('Value for S-1006'), 'high');
    await user.click(screen.getByRole('button', { name: 'Save resolution' }));

    expect(onSave).toHaveBeenCalledWith({
      fingerprint: conflict.fingerprint,
      choice: 'custom',
      values: { 'S-1004': 'low', 'S-1005': 'medium', 'S-1006': 'high' },
    });
  });

  it('fills every member with one value and lets a member differ afterwards', async () => {
    const conflict = members();
    const { onSave, user } = setup(conflict, { spec: bandSpec });
    await user.click(screen.getByRole('radio', { name: 'Custom value' }));
    await user.selectOptions(screen.getByLabelText('Same value for every member'), 'medium');
    await user.click(screen.getByRole('button', { name: 'Apply to all members' }));
    expect(screen.getByLabelText('Value for S-1006')).toHaveValue('medium');

    await user.selectOptions(screen.getByLabelText('Value for S-1006'), 'high');
    await user.click(screen.getByRole('button', { name: 'Save resolution' }));
    expect(onSave).toHaveBeenCalledWith({
      fingerprint: conflict.fingerprint,
      choice: 'custom',
      values: { 'S-1004': 'medium', 'S-1005': 'medium', 'S-1006': 'high' },
    });
  });

  it('saves a bulk choice for the whole group without values', async () => {
    const conflict = members();
    const { onSave, user } = setup(conflict, { spec: bandSpec });
    await user.click(screen.getByRole('radio', { name: 'Relax the rule' }));
    await user.click(screen.getByRole('button', { name: 'Save resolution' }));
    expect(onSave).toHaveBeenCalledWith({ fingerprint: conflict.fingerprint, choice: 'relax' });
  });

  it('lists the members with their values when the sides are absent', () => {
    setup(members(), { spec: bandSpec });
    const table = screen.getByRole('table', { name: 'Members of XS4' });
    expect(within(table).getAllByRole('row')).toHaveLength(4);
    expect(within(table).getByText('S-1005')).toBeInTheDocument();
  });
});

describe('diffRows', () => {
  it('compares Records attribute by attribute and marks the sides that moved', () => {
    const rows = diffRows({
      base: { name: 'Monza Foundry S.r.l.', city: 'Monza' },
      ours: { name: 'Monza Foundry', city: 'Monza' },
      theirs: null,
    });
    const byPath = Object.fromEntries(rows.map((r) => [r.path, r]));
    expect(byPath['name']?.changed).toEqual({ ours: true, theirs: true });
    expect(byPath['city']?.changed).toEqual({ ours: false, theirs: true });
    expect(byPath['city']?.theirs).toBeUndefined();
  });

  it('flattens schema documents into dotted paths', () => {
    const rows = diffRows({
      base: { constraints: { max: 100 } },
      ours: { constraints: { max: 100 } },
      theirs: { constraints: { max: 60 }, indexed: true },
    });
    expect(rows.map((r) => r.path)).toEqual(['constraints.max', 'indexed']);
    expect(rows[0]?.changed).toEqual({ ours: false, theirs: true });
  });
});
