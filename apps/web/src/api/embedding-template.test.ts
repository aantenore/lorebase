import { renamePlaceholder, templateFields, templateProblems } from './embedding-template';
import { asRemote, createMockApi } from './mock/adapter';
import { validateSchema } from './schema-rules';
import type { Kind, Schema } from './types';

async function sample(): Promise<Schema> {
  const api = asRemote(createMockApi({ pageSize: 50, searchLimit: 20, maxNodes: 200 }), 0);
  return api.schema.get({ domain: 'supply', branch: 'main' });
}

async function supplier(): Promise<Kind> {
  const found = (await sample()).kinds.find((k) => k.api_name === 'Supplier');
  if (found === undefined) throw new Error('No Supplier');
  return found;
}

const withTemplate = (kind: Kind, template: string | null, sensitive: boolean | null = null) => ({
  ...kind,
  search: { ...kind.search, embedding_template: template, embedding_sensitive: sensitive },
});

describe('embedding template', () => {
  it('lists placeholders in order of first use and honors escaped braces', () => {
    expect(templateFields('{a} and {b}, {a} {{literal}} {_key}')).toEqual(['a', 'b', '_key']);
  });

  it('refuses malformed text', () => {
    expect(() => templateFields('{a')).toThrow('unbalanced');
    expect(() => templateFields('a}')).toThrow('unbalanced');
    expect(() => templateFields('{ }')).toThrow('empty placeholder');
    expect(() => templateFields('no placeholders')).toThrow('names no Attribute');
  });

  it('accepts Attributes of the Kind and the special fields', async () => {
    const kind = await supplier();
    expect(templateProblems(withTemplate(kind, '{legal_name} ({_kind} {_key})'))).toEqual([]);
    expect(templateProblems(withTemplate(kind, null))).toEqual([]);
    expect(templateProblems(withTemplate(kind, '  '))).toEqual([]);
  });

  it('names the placeholders that are not Attributes of the Kind', async () => {
    const kind = await supplier();
    expect(templateProblems(withTemplate(kind, '{legal_name} {nope}'))).toEqual([
      "{nope} is not an Attribute of Kind 'Supplier'",
    ]);
    expect(templateProblems(withTemplate(kind, '{legal_name'))).toHaveLength(1);
  });

  it('refuses sensitive Attributes unless the Kind allows them', async () => {
    const kind = await supplier();
    const first = Object.values(kind.attributes)[0];
    if (first === undefined) throw new Error('No Attribute');
    const sensitive: Kind = {
      ...kind,
      attributes: { ...kind.attributes, [first.id]: { ...first, sensitivity: ['pii'] } },
    };
    const template = `{${first.api_name}}`;
    expect(templateProblems(withTemplate(sensitive, template))[0]).toContain('is sensitive (pii)');
    expect(templateProblems(withTemplate(sensitive, template, true))).toEqual([]);
  });

  it('reports a bad template at the path of the Kind search block', async () => {
    const schema = await sample();
    const [first, ...rest] = schema.kinds;
    if (first === undefined) throw new Error('No Kind');
    const broken = { ...schema, kinds: [withTemplate(first, '{nope}'), ...rest] };
    const issues = validateSchema(broken).filter((i) => i.code === 'embedding_template_invalid');
    expect(issues.map((i) => i.path)).toEqual([
      `kinds/${first.api_name}/search/embedding_template`,
    ]);
  });

  it('renames a placeholder and leaves escaped braces alone', () => {
    expect(renamePlaceholder('{old}, { old } {{old}} {other}', 'old', 'new')).toBe(
      '{new}, {new} {{old}} {other}',
    );
  });
});
