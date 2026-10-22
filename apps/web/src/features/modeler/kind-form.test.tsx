import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { SchemaDocument } from './document';
import { renderForm } from './form-harness';
import { KindsForm } from './kind-form';

const document = (): SchemaDocument => ({
  kinds: {
    Supplier: {
      id: 'knd_supplier',
      primary_key: ['code'],
      title_attribute: 'name',
      attributes: { code: { type: 'string' }, name: { type: 'string' } },
      search: { embedding_template: '{name}', embedding_sensitive: true },
    },
    Part: {
      primary_key: ['code'],
      title_attribute: 'code',
      attributes: { code: { type: 'string' } },
    },
  },
});

const SENSITIVE = 'Allow sensitive Attributes in the template';

describe('Kind editor, semantic search', () => {
  it('shows the template and the sensitive switch of the Kind', () => {
    renderForm(KindsForm, document());
    expect(screen.getByRole('textbox', { name: 'Embedding template' })).toHaveValue('{name}');
    expect(screen.getByRole('checkbox', { name: SENSITIVE })).toBeChecked();
  });

  it('writes the template and keeps the other search keys', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(KindsForm, document());
    const field = screen.getByRole('textbox', { name: 'Embedding template' });
    await user.clear(field);
    await user.type(field, '{{code} {{{{x');
    expect(current().kinds?.['Supplier']?.search).toEqual({
      embedding_template: '{code} {{x',
      embedding_sensitive: true,
    });
  });

  it('drops the search block when both values are cleared', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(KindsForm, document());
    await user.clear(screen.getByRole('textbox', { name: 'Embedding template' }));
    await user.click(screen.getByRole('checkbox', { name: SENSITIVE }));
    expect(current().kinds?.['Supplier']).not.toHaveProperty('search');
  });

  it('leaves a Kind without a template untouched until a value is set', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(KindsForm, document());
    await user.selectOptions(screen.getByRole('combobox', { name: 'Kind' }), 'Part');
    expect(current().kinds?.['Part']).not.toHaveProperty('search');
    await user.click(screen.getByRole('checkbox', { name: SENSITIVE }));
    expect(current().kinds?.['Part']?.search).toEqual({ embedding_sensitive: true });
  });

  it('shows the problem the schema check found in the template', () => {
    renderForm(KindsForm, document(), [
      {
        path: 'kinds/Supplier/search/embedding_template',
        code: 'embedding_template_invalid',
        message: 'Embedding template of Kind Supplier: {nope} is not an Attribute.',
      },
    ]);
    expect(screen.getByText(/\{nope\} is not an Attribute/)).toBeInTheDocument();
  });
});
