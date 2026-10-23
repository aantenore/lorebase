import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { messages } from '@/messages';
import { DerivationsForm } from './derivation-form';
import type { SchemaDocument } from './document';
import { celView, renderForm, typeCel } from './form-harness';

const document = (): SchemaDocument => ({
  kinds: {
    Part: {
      primary_key: ['part_number'],
      attributes: {
        part_number: { type: 'string' },
        supplier_count: { type: 'integer' },
        single_sourced: { type: 'boolean', derivation: 'single_sourced' },
      },
    },
    Supplier: {
      primary_key: ['code'],
      attributes: { code: { type: 'string' }, risk_score: { type: 'integer' } },
    },
    Plant: { primary_key: ['code'], attributes: { code: { type: 'string' } } },
  },
  relations: {
    supplied_by: { inverse_api_name: 'supplies', from_kind: 'Part', to_kind: 'Supplier' },
    feeds: { inverse_api_name: 'fed_by', from_kind: 'Supplier', to_kind: 'Plant' },
  },
  derivations: {
    single_sourced: {
      id: 'drv_single',
      description: 'True when a part has exactly one supplier.',
      definition: {
        variant: 'expression',
        kind: 'Part',
        attribute: 'single_sourced',
        expression: 'supplier_count == 1',
        x_engine: 'kept',
      },
      x_owner: 'planning',
    },
  },
});

describe('Derivation editor', () => {
  it('edits the expression and keeps every key it does not edit', async () => {
    const { current } = renderForm(DerivationsForm, document());
    expect((await celView('Expression')).state.doc.toString()).toBe('supplier_count == 1');
    await typeCel('Expression', 'supplier_count <= 1');
    expect(current().derivations?.['single_sourced']).toEqual({
      id: 'drv_single',
      description: 'True when a part has exactly one supplier.',
      definition: {
        variant: 'expression',
        kind: 'Part',
        attribute: 'single_sourced',
        expression: 'supplier_count <= 1',
        x_engine: 'kept',
      },
      x_owner: 'planning',
    });
  });

  it('moves the target Attribute and marks the new one as derived', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(DerivationsForm, document());
    const target = screen.getByRole('combobox', { name: 'Target Attribute' });
    expect(target).toHaveValue('single_sourced');
    await user.selectOptions(target, 'supplier_count');
    const part = current().kinds?.['Part']?.attributes;
    expect(part?.['supplier_count']?.derivation).toBe('single_sourced');
    expect(part?.['single_sourced']?.derivation).toBeUndefined();
    expect(current().derivations?.['single_sourced']?.['definition']).toMatchObject({
      kind: 'Part',
      attribute: 'supplier_count',
    });
  });

  it('turns an expression into an aggregate over linked Records', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(DerivationsForm, document());
    await user.selectOptions(screen.getByRole('combobox', { name: 'Variant' }), 'aggregate');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Function' }), 'max');
    // The far end of supplied_by is Supplier: its Attributes are the choices.
    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Source Attribute' }),
      'risk_score',
    );
    await typeCel('Filter', 'risk_score > 0');
    expect(current().derivations?.['single_sourced']?.['definition']).toEqual({
      variant: 'aggregate',
      kind: 'Part',
      attribute: 'single_sourced',
      x_engine: 'kept',
      relation: 'supplied_by',
      function: 'max',
      source_attribute: 'risk_score',
      filter: 'risk_score > 0',
    });
  });

  it('builds a path from Relation steps and releases the target Attribute', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(DerivationsForm, document());
    await user.selectOptions(screen.getByRole('combobox', { name: 'Variant' }), 'path');
    expect(screen.getByRole('combobox', { name: 'Path step 1 Relation' })).toHaveValue(
      'supplied_by',
    );
    expect(screen.getByRole('combobox', { name: 'Path step 2 Relation' })).toHaveValue('feeds');
    await user.click(
      screen.getByRole('checkbox', { name: messages.modelerEditors.derivation.stored }),
    );
    expect(current().derivations?.['single_sourced']?.['definition']).toEqual({
      variant: 'path',
      x_engine: 'kept',
      steps: [{ relation: 'supplied_by' }, { relation: 'feeds' }],
      materialized: true,
    });
    expect(current().kinds?.['Part']?.attributes?.['single_sourced']?.derivation).toBeUndefined();
  });

  it('shows validation and type check errors next to their fields', async () => {
    renderForm(DerivationsForm, document(), [
      {
        path: 'derivations/single_sourced/definition/expression',
        code: 'schema.expression_invalid',
        message: 'expression: expected bool, got int',
      },
      {
        path: 'derivations/single_sourced/definition/attribute',
        code: 'derivation_not_declared',
        message: 'Attribute single_sourced does not declare this Derivation.',
      },
    ]);
    expect((await celView('Expression')).contentDOM).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByText('expression: expected bool, got int')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Target Attribute' })).toHaveAccessibleDescription(
      'Attribute single_sourced does not declare this Derivation.',
    );
  });

  it('renames a Derivation and the Attribute that declares it follows', async () => {
    const user = userEvent.setup();
    const { current } = renderForm(DerivationsForm, document());
    const name = screen.getByRole('textbox', { name: 'Derivation name' });
    await user.clear(name);
    await user.type(name, 'one_supplier');
    expect(Object.keys(current().derivations ?? {})).toEqual(['one_supplier']);
    expect(current().kinds?.['Part']?.attributes?.['single_sourced']?.derivation).toBe(
      'one_supplier',
    );
  });
});
