/**
 * Embedding templates (design 8.4): the text of a Record that Scribe embeds. A Kind opts into
 * semantic search with `search.embedding_template`, a text with `{attribute}` placeholders
 * naming Attributes of the Kind by api name, plus `{_kind}` and `{_key}`; `{{` and `}}` write
 * literal braces. This mirrors the Keeper check (`lorebase_core.model.embedding`), so the
 * Modeler reports the same problems before a change set is sent.
 */
import type { Kind } from './types';

/** Placeholders every Kind can use besides its Attributes. */
export const SPECIAL_FIELDS: readonly string[] = ['_kind', '_key'];

const TOKEN = /\{\{|\}\}|\{([^{}]*)\}|[{}]/g;

export class TemplateError extends Error {}

/** Placeholder names in order of first use; throws `TemplateError` on malformed text. */
export function templateFields(source: string): string[] {
  const fields: string[] = [];
  for (const match of source.matchAll(TOKEN)) {
    const token = match[0];
    if (token === '{{' || token === '}}') continue;
    const inner = match[1];
    if (inner === undefined) {
      throw new TemplateError(
        `unbalanced '${token}' at offset ${match.index}; write ${token}${token}`,
      );
    }
    const name = inner.trim();
    if (name === '') throw new TemplateError(`empty placeholder at offset ${match.index}`);
    if (!fields.includes(name)) fields.push(name);
  }
  if (fields.length === 0) throw new TemplateError('the template names no Attribute');
  return fields;
}

/** `source` with the placeholder `{from}` renamed to `{to}`; escaped braces are untouched. */
export function renamePlaceholder(source: string, from: string, to: string): string {
  return source.replace(/\{\{|\}\}|\{([^{}]*)\}/g, (token, inner: string | undefined) =>
    inner !== undefined && inner.trim() === from ? `{${to}}` : token,
  );
}

/** Why the Kind's template cannot be used; empty when it can, or when there is none. */
export function templateProblems(kind: Kind): string[] {
  const source = kind.search.embedding_template;
  if (source === null || source.trim() === '') return [];
  let fields: string[];
  try {
    fields = templateFields(source);
  } catch (error) {
    return [error instanceof TemplateError ? error.message : String(error)];
  }
  const attributes = Object.values(kind.attributes);
  const allowSensitive = kind.search.embedding_sensitive === true;
  const problems: string[] = [];
  for (const name of fields) {
    if (SPECIAL_FIELDS.includes(name)) continue;
    const attribute = attributes.find((a) => a.api_name === name);
    if (attribute === undefined) {
      problems.push(`{${name}} is not an Attribute of Kind '${kind.api_name}'`);
    } else if (attribute.derivation_id !== null) {
      problems.push(`{${name}} is derived; templates read stored values only`);
    } else if (attribute.sensitivity.length > 0 && !allowSensitive) {
      problems.push(
        `{${name}} is sensitive (${attribute.sensitivity.join(', ')}); ` +
          'set search.embedding_sensitive to embed it',
      );
    }
  }
  return problems;
}
