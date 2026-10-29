import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { makeConflict } from '@/api/mock/conflicts';
import type { Conflict, Resolution } from '@/api/types';
import type { ValueSpec } from '@/api/values';
import { ConflictResolver } from './conflict-resolver';

const hunks = [
  { status: 'clean', base: 'Intro. ', ours: 'Intro. ', theirs: 'Intro! ', merged: 'Intro! ' },
  {
    status: 'conflict',
    base: 'One was late. ',
    ours: 'One was late by nine days. ',
    theirs: 'Three were late. ',
    merged: null,
  },
  {
    status: 'conflict',
    base: 'No backup.',
    ours: 'No backup; stock for four weeks.',
    theirs: 'A backup is coming.',
    merged: null,
  },
];

const side = (key: 'base' | 'ours' | 'theirs') => hunks.map((h) => h[key]).join('');

const reason = (): Conflict =>
  makeConflict({
    kind: 'DV1',
    partition: 'RiskFlag',
    key: 'F-1',
    attribute: 'att_reason',
    base: side('base'),
    ours: side('ours'),
    theirs: side('theirs'),
    message: 'The reason changed on both Branches.',
    details: { text_hunks: hunks },
  });

const spec: ValueSpec = { type: 'text', constraints: { max_length: 40 } };

function setup(conflict: Conflict, resolution?: Resolution) {
  const onSave = vi.fn();
  const onDirtyChange = vi.fn();
  const view = (r?: Resolution) => (
    <ConflictResolver
      conflict={conflict}
      resolution={r}
      names={new Map()}
      spec={spec}
      branches={{ ours: 'main', theirs: 'model/risk' }}
      onSave={onSave}
      onDirtyChange={onDirtyChange}
    />
  );
  const utils = render(view(resolution));
  return { onSave, onDirtyChange, user: userEvent.setup(), view, ...utils };
}

const preview = () => screen.getByRole('heading', { name: 'Composed text' }).parentElement;
const passage = (n: number) =>
  screen.getByRole('group', { name: `Conflicting passage ${n.toString()} of 2` });
const pick = async (user: ReturnType<typeof userEvent.setup>, n: number, name: string) => {
  await user.click(within(passage(n)).getByRole('radio', { name }));
};

describe('Text passage view', () => {
  it('shows each conflicting passage with the Branch names and the unchanged text apart', () => {
    setup(reason());
    const view = screen.getByRole('region', { name: 'Text by passage' });
    expect(view.querySelector('p.text-muted')).toHaveTextContent('Intro!');
    expect(within(passage(1)).getByText('Ours (main)')).toBeInTheDocument();
    expect(within(passage(1)).getByText('Theirs (model/risk)')).toBeInTheDocument();
    expect(passage(2).querySelector('[data-side="ours"] ins')).not.toBeNull();
    expect(screen.getByText('2 of 2 passages open')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Use merged text' })).toBeDisabled();
  });

  it('composes the picks live and saves them as a custom value', async () => {
    const conflict = reason();
    const { onSave, user } = setup(conflict);
    await pick(user, 1, 'Theirs');
    expect(screen.getByText('1 of 2 passages open')).toBeInTheDocument();
    expect(screen.getByText('[passage 2 is open]')).toBeInTheDocument();

    await pick(user, 2, 'Both, ours then theirs');
    const preview = screen.getByRole('heading', { name: 'Composed text' }).parentElement;
    expect(screen.getByRole('status')).toHaveTextContent('Every passage has a choice.');
    expect(preview).not.toHaveAttribute('aria-live');
    const composed = 'Intro! Three were late. No backup; stock for four weeks. A backup is coming.';
    expect(preview).toHaveTextContent(composed);
    expect(screen.getByText('Every passage has a choice.')).toBeInTheDocument();
    expect(screen.getByText(`${composed.length.toString()} of 40 characters`)).toBeInTheDocument();
    expect(
      screen.getByText(/Does not fit the Attribute: Must have at most 40/),
    ).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Use merged text' }));
    expect(onSave).toHaveBeenCalledWith({
      fingerprint: conflict.fingerprint,
      choice: 'custom',
      value: composed,
    });
  });

  it('edits a passage in place, seeded with the current pick', async () => {
    const { user } = setup(reason());
    await pick(user, 1, 'Theirs');
    await pick(user, 1, 'Edit');
    const editor = screen.getByRole('textbox', { name: 'Edited text of passage 1' });
    expect(editor).toHaveValue('Three were late. ');
    await user.clear(editor);
    await user.type(editor, 'Late.');
    await pick(user, 2, 'Base');
    expect(preview()).toHaveTextContent('Intro! Late.No backup.');
  });

  it('works from the keyboard outside text fields', async () => {
    const { user } = setup(reason());
    await user.keyboard('1');
    expect(within(passage(1)).getByRole('radio', { name: 'Ours' })).toBeChecked();
    await user.keyboard(']');
    expect(passage(2)).toHaveFocus();
    await user.keyboard('4');
    expect(within(passage(2)).getByRole('radio', { name: 'Both, theirs then ours' })).toBeChecked();
    await user.keyboard('[[');
    await user.keyboard('0');
    expect(within(passage(1)).getByRole('radio', { name: 'Base' })).toBeChecked();

    // The whole-value keys keep their meaning.
    await user.keyboard('t');
    expect(screen.getByRole('radio', { name: 'Take theirs' })).toBeChecked();
  });

  it('does not take the number keys while editing a passage', async () => {
    const { user } = setup(reason());
    await pick(user, 1, 'Edit');
    await user.type(screen.getByRole('textbox', { name: 'Edited text of passage 1' }), '2');
    expect(within(passage(1)).getByRole('radio', { name: 'Edit' })).toBeChecked();
  });

  it('counts picks as unsaved until the composed text is stored', async () => {
    const conflict = reason();
    const { onDirtyChange, user, view, rerender } = setup(conflict);
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
    await pick(user, 1, 'Ours');
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
    await pick(user, 2, 'Ours');
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
    rerender(
      view({
        fingerprint: conflict.fingerprint,
        choice: 'custom',
        value: 'Intro! One was late by nine days. No backup; stock for four weeks.',
      }),
    );
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
  });

  it('shows a stored custom text as resolved', () => {
    const conflict = reason();
    setup(conflict, { fingerprint: conflict.fingerprint, choice: 'custom', value: 'Own text.' });
    expect(screen.getByText('A custom text is saved for this Conflict.')).toBeInTheDocument();
    expect(preview()).toHaveTextContent('Own text.');
  });

  it('falls back to the side-by-side table without passages', () => {
    setup({ ...reason(), details: {} });
    expect(screen.queryByRole('region', { name: 'Text by passage' })).not.toBeInTheDocument();
    expect(screen.getByRole('table', { name: 'Base, ours and theirs' })).toBeInTheDocument();
  });
});

describe('Text passage view saves one way', () => {
  it('saves the composed text with s, not a whole-value choice picked before', async () => {
    const conflict = reason();
    const { onSave, user } = setup(conflict, { fingerprint: conflict.fingerprint, choice: 'ours' });
    await pick(user, 1, 'Ours');
    expect(screen.getByRole('button', { name: 'Update resolution' })).toBeDisabled();
    await user.keyboard('s');
    expect(onSave).not.toHaveBeenCalled();

    await pick(user, 2, 'Theirs');
    await user.keyboard('s');
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave).toHaveBeenCalledWith({
      fingerprint: conflict.fingerprint,
      choice: 'custom',
      value: 'Intro! One was late by nine days. A backup is coming.',
    });
    await user.click(screen.getByRole('button', { name: 'Update resolution' }));
    expect(onSave).toHaveBeenCalledTimes(2);
  });

  it('drops the picks when a whole-value choice is made', async () => {
    const conflict = reason();
    const { onSave, user } = setup(conflict);
    await pick(user, 1, 'Ours');
    await user.click(screen.getByRole('radio', { name: 'Take theirs' }));
    expect(within(passage(1)).getByRole('radio', { name: 'Ours' })).not.toBeChecked();
    await user.click(screen.getByRole('button', { name: 'Save resolution' }));
    expect(onSave).toHaveBeenCalledWith({ fingerprint: conflict.fingerprint, choice: 'theirs' });

    await pick(user, 2, 'Base');
    await user.keyboard('o');
    expect(within(passage(2)).getByRole('radio', { name: 'Base' })).not.toBeChecked();
  });

  it('adopts the stored composed text as the draft after saving it', async () => {
    const conflict = reason();
    const { onDirtyChange, user, view, rerender } = setup(conflict);
    await user.click(screen.getByRole('radio', { name: 'Take ours' }));
    await pick(user, 1, 'Ours');
    await pick(user, 2, 'Base');
    rerender(
      view({
        fingerprint: conflict.fingerprint,
        choice: 'custom',
        value: 'Intro! One was late by nine days. No backup.',
      }),
    );
    expect(screen.getByRole('radio', { name: 'Custom value' })).toBeChecked();
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
  });

  it('keeps the passage view away from a Conflict that takes no custom value', () => {
    setup({ ...reason(), choices: ['ours', 'theirs', 'base'] });
    expect(screen.queryByRole('region', { name: 'Text by passage' })).not.toBeInTheDocument();
  });
});

describe('Passage keys with one conflicting passage', () => {
  const single = (): Conflict =>
    makeConflict({
      kind: 'DV1',
      partition: 'RiskFlag',
      key: 'F-1',
      attribute: 'att_reason',
      base: 'Same. Old.',
      ours: 'Same. New.',
      theirs: 'Same. Newer.',
      message: 'm',
      details: {
        text_hunks: [
          { status: 'clean', base: 'Same. ', ours: 'Same. ', theirs: 'Same. ', merged: 'Same. ' },
          { status: 'conflict', base: 'Old.', ours: 'New.', theirs: 'Newer.', merged: null },
        ],
      },
    });

  it('focuses the passage at once and leaves no later focus request behind', async () => {
    const { user } = setup(single());
    const card = screen.getByRole('group', { name: 'Conflicting passage 1 of 1' });
    await user.keyboard(']');
    expect(card).toHaveFocus();
    screen.getByRole('radio', { name: 'Take ours' }).focus();
    await user.keyboard('1');
    expect(screen.getByRole('radio', { name: 'Take ours' })).toHaveFocus();
  });
});
