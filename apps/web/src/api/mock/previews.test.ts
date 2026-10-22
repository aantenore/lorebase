import { evaluateRowExpression, previewExpressions } from './previews';

describe('mock expression previews', () => {
  it('evaluates column references, literals and +', () => {
    const row = { first: 'Ada', last: 'Lovelace', n: 2, 'Legal name': 'Engines' };
    expect(evaluateRowExpression('first + " " + last', row)).toBe('Ada Lovelace');
    expect(evaluateRowExpression('row["Legal name"]', row)).toBe('Engines');
    expect(evaluateRowExpression('n + 3', row)).toBe(5);
    expect(evaluateRowExpression("'a+b' + first", row)).toBe('a+bAda');
    expect(() => evaluateRowExpression('missing', row)).toThrow(/undeclared reference/);
    expect(() => evaluateRowExpression('size(first)', row)).toThrow(/Keeper evaluates CEL/);
  });

  it('answers per row and Attribute, with the errors as cells', () => {
    const document = {
      sources: {
        s: {
          attribute_mapping: {
            name: { expression: 'a + b' },
            city: { column: 'city' },
            note: { expression: 'c' },
          },
        },
      },
    };
    const out = previewExpressions({
      document,
      source: 's',
      rows: [
        { a: 'x', b: 'y', c: 'z' },
        { a: 'x', b: 'y' },
      ],
    });
    expect(out.attributes).toEqual(['name', 'note']);
    expect(out.rows[0]).toEqual({
      name: { value: 'xy', error: null },
      note: { value: 'z', error: null },
    });
    expect(out.rows[1]?.['note']?.error?.message).toMatch(/undeclared/);
  });
});
