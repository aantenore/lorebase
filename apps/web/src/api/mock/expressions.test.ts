import { syntaxProblem } from './expressions';

describe('mock expression check', () => {
  it('accepts well formed CEL and names the first syntax problem', () => {
    expect(syntaxProblem('record(params.supplier).status != "terminated"')).toBeNull();
    expect(syntaxProblem("size(x) == 1 && y['a'] in [1, 2]")).toBeNull();
    expect(syntaxProblem('  ')).toBe('the expression is empty');
    expect(syntaxProblem('size(x == 1')).toBe('missing closing bracket for (');
    expect(syntaxProblem('x]')).toBe('unexpected ] at column 2');
    expect(syntaxProblem('"open')).toBe('unterminated string at column 1');
    expect(syntaxProblem('a &&')).toBe('the expression ends early');
  });
});
