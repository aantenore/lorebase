import {
  changeTransformName,
  nullIfValues,
  scalarFrom,
  scalarText,
  transformProblem,
  TRANSFORM_NAMES,
} from './transform-edits';
import { moveItem } from './form-helpers';

describe('transform edits', () => {
  it('starts each transform with the arguments Scribe requires', () => {
    for (const name of TRANSFORM_NAMES) {
      const transform: Record<string, unknown> = { name: 'trim', x_note: 'kept' };
      changeTransformName(transform, name);
      expect(transformProblem(transform)).toBeUndefined();
      expect(transform['x_note']).toBe('kept');
    }
  });

  it('drops the arguments of the previous name', () => {
    const transform: Record<string, unknown> = { name: 'cast', args: { to: 'integer' } };
    changeTransformName(transform, 'upper');
    expect(transform).toEqual({ name: 'upper' });
  });

  it('reports what Scribe would refuse', () => {
    expect(transformProblem({ name: 'cast', args: { to: 'money' } })).toMatch(/cast needs/);
    expect(transformProblem({ name: 'parse_date' })).toBe('parse_date needs a format.');
    expect(transformProblem({ name: 'constant', args: { value: null } })).toBeUndefined();
    expect(transformProblem({ name: 'rot13' })).toBe('Unknown transform rot13.');
  });

  it('keeps the type of a scalar while the text still is that type', () => {
    expect(scalarText(5)).toBe('5');
    expect(scalarText(null)).toBe('null');
    expect(scalarFrom('7', 5)).toBe(7);
    expect(scalarFrom('true', false)).toBe(true);
    expect(scalarFrom('seven', 5)).toBe('seven');
    expect(scalarFrom('7', 'text')).toBe('7');
  });

  it('reads null_if from values or from the single value', () => {
    expect(nullIfValues({ values: ['n/a', ''] })).toEqual(['n/a', '']);
    expect(nullIfValues({ value: 'n/a' })).toEqual(['n/a']);
    expect(nullIfValues({})).toEqual([]);
  });

  it('moves an item and ignores moves out of range', () => {
    expect(moveItem(['a', 'b', 'c'], 0, 2)).toEqual(['b', 'c', 'a']);
    expect(moveItem(['a', 'b', 'c'], 2, 1)).toEqual(['a', 'c', 'b']);
    const list = ['a', 'b'];
    expect(moveItem(list, 0, -1)).toBe(list);
    expect(moveItem(list, 1, 2)).toBe(list);
  });
});
