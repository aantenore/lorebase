import {
  canonicalNumber,
  numberOf,
  parseAttributeType,
  validateValue,
  type ParsedType,
} from './values';

const type = (text: string): ParsedType => {
  const parsed = parseAttributeType(text);
  if (parsed === null) throw new Error(`bad type ${text}`);
  return parsed;
};

describe('canonicalNumber', () => {
  it('writes integers without sign or leading zeros', () => {
    expect(canonicalNumber('055', type('integer'))).toBe('55');
    expect(canonicalNumber('+7', type('integer'))).toBe('7');
    expect(canonicalNumber('-12', type('integer'))).toBe('-12');
    expect(canonicalNumber('9007199254740993', type('integer'))).toBe('9007199254740993');
  });

  it('pads decimals to the scale of the type', () => {
    expect(canonicalNumber('9.5', type('decimal(10,2)'))).toBe('9.50');
    expect(canonicalNumber('90', type('decimal(10,2)'))).toBe('90.00');
    expect(canonicalNumber('.5', type('decimal(8,3)'))).toBe('0.500');
    expect(canonicalNumber('-0.00', type('decimal(10,2)'))).toBe('0.00');
    expect(canonicalNumber('3', type('decimal(5,0)'))).toBe('3');
  });

  it('leaves text that is not a number of the type for validation', () => {
    expect(canonicalNumber('1.234', type('decimal(10,2)'))).toBe('1.234');
    expect(canonicalNumber('abc', type('integer'))).toBe('abc');
    expect(canonicalNumber('1.5', type('integer'))).toBe('1.5');
  });
});

describe('numeric values', () => {
  it('accepts the canonical strings Keeper writes and plain numbers', () => {
    expect(numberOf('32', true)).toBe(32);
    expect(numberOf(32, true)).toBe(32);
    expect(numberOf('24.01', false)).toBe(24.01);
    expect(numberOf('1e3', false)).toBeNull();
    expect(numberOf('', true)).toBeNull();
    expect(validateValue({ type: 'integer', constraints: { max: 100 } }, '32')).toBeNull();
    expect(validateValue({ type: 'integer', constraints: { max: 100 } }, '132')).toBe(
      'Must be at most 100.',
    );
    expect(validateValue({ type: 'integer' }, '1.5')).toBe('Must be a whole number.');
    expect(validateValue({ type: 'decimal(10,2)' }, '9.50')).toBeNull();
    expect(validateValue({ type: 'float' }, 1.5)).toBeNull();
    expect(validateValue({ type: 'float' }, '1.5')).toBe('Must be a number.');
  });
});
