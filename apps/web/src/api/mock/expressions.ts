/**
 * The mock adapter's stand-in for Keeper's CEL check (`schema.expressions_enabled`): a syntax
 * check of every Operation and Derivation expression (balanced brackets, closed strings, no
 * dangling operator) and, in Operations, `params.<name>` must name a parameter. Findings
 * use Keeper's code and document paths, so the Modeler shows them where Keeper's would be.
 */
import type { Derivation, Operation, Violation } from '../types';
import type { SchemaBody } from '../schema-rules';

export const EXPRESSION_INVALID = 'schema.expression_invalid';

const PAIRS: Record<string, string> = { ')': '(', ']': '[', '}': '{' };

/** The syntax problem of `source`, or `null`. */
export function syntaxProblem(source: string): string | null {
  const text = source.trim();
  if (text === '') return 'the expression is empty';
  const stack: string[] = [];
  for (let i = 0; i < text.length; i += 1) {
    const c = text.charAt(i);
    if (c === '"' || c === "'") {
      let j = i + 1;
      while (j < text.length && text.charAt(j) !== c) j += text.charAt(j) === '\\' ? 2 : 1;
      if (j >= text.length) return `unterminated string at column ${i + 1}`;
      i = j;
    } else if (c === '(' || c === '[' || c === '{') {
      stack.push(c);
    } else if (c in PAIRS) {
      if (stack.pop() !== PAIRS[c]) return `unexpected ${c} at column ${i + 1}`;
    }
  }
  if (stack.length > 0) return `missing closing bracket for ${stack[stack.length - 1] ?? ''}`;
  if (/(&&|\|\||==|!=|<=|>=|[-+*/%<>!?:.])$/.test(text)) return 'the expression ends early';
  return null;
}

function check(
  out: Violation[],
  path: string,
  source: unknown,
  parameters: ReadonlySet<string> | null,
) {
  if (typeof source !== 'string') return;
  const problem = syntaxProblem(source);
  if (problem !== null) {
    out.push({ code: EXPRESSION_INVALID, message: `expression: ${problem}`, path });
    return;
  }
  if (parameters === null) return;
  for (const match of source.matchAll(/\bparams\.([A-Za-z_]\w*)/g)) {
    const name = match[1] ?? '';
    if (!parameters.has(name)) {
      out.push({
        code: EXPRESSION_INVALID,
        message: `expression: undeclared reference to 'params.${name}'`,
        path,
      });
    }
  }
}

function checkOperation(out: Violation[], operation: Operation) {
  const path = `operations/${operation.api_name}`;
  const parameters = new Set(operation.parameters.map((p) => p.name));
  operation.preconditions.forEach((p, index) => {
    check(out, `${path}/preconditions/${index}/expression`, p.expression, parameters);
  });
  const policy = operation.review_policy;
  if (policy !== 'never' && policy !== 'always') {
    check(out, `${path}/review_policy/expression`, policy, parameters);
  }
  operation.steps.forEach((step, index) => {
    const at = `${path}/steps/${index}`;
    for (const key of ['target', 'from', 'to'])
      check(out, `${at}/${key}`, step.with[key], parameters);
    const values = step.with['values'];
    if (typeof values === 'object' && values !== null) {
      for (const [attribute, expression] of Object.entries(values)) {
        check(out, `${at}/values/${attribute}`, expression, parameters);
      }
    }
  });
}

function checkDerivation(out: Violation[], derivation: Derivation) {
  const at = `derivations/${derivation.api_name}/definition`;
  if (derivation.variant === 'expression')
    check(out, `${at}/expression`, derivation.definition['expression'], null);
  if (derivation.variant === 'aggregate' && derivation.definition['filter'] !== undefined) {
    check(out, `${at}/filter`, derivation.definition['filter'], null);
  }
}

/** Expression findings of the custom Operations and the Derivations of `schema`. */
export function expressionViolations(schema: SchemaBody): Violation[] {
  const out: Violation[] = [];
  for (const operation of schema.operations) if (!operation.builtin) checkOperation(out, operation);
  for (const derivation of schema.derivations) checkDerivation(out, derivation);
  return out;
}
