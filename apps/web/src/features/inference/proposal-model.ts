/**
 * The edited schema proposal as plain functions over a schema document: which elements are
 * accepted or dropped, which of them can be applied together, and how they merge into the
 * schema of the Branch. No React here, so the rules are testable on their own.
 */
import type { InferenceEvidence } from '@/api/inference-types';
import type {
  AttributeDocument,
  IdFactory,
  KindDocument,
  RelationDocument,
  SchemaDocument,
  TypeDocument,
} from '@/features/modeler/document';
import { renameKey } from '@/features/modeler/form-helpers';

export type Decision = 'accepted' | 'dropped';
/** Decisions by element id. An element without an entry is pending. */
export type Decisions = Readonly<Record<string, Decision>>;
export type EvidenceIndex = ReadonlyMap<string, InferenceEvidence>;

/** Comparing confidences that went through a percentage field needs a little room. */
const EPSILON = 1e-9;

export const idOf = (element: { id?: string | undefined }): string => element.id ?? '';

export function indexEvidence(list: readonly InferenceEvidence[]): EvidenceIndex {
  return new Map(list.map((entry) => [entry.element_id, entry]));
}

// ------------------------------------------------------------------ elements

const ID_PREFIX = { kind: 'knd', attribute: 'att', relation: 'rel' } as const;

function fillAttributeIds(
  attributes: Record<string, AttributeDocument> | undefined,
  scope: string,
  ids: IdFactory,
) {
  for (const [name, attribute] of Object.entries(attributes ?? {})) {
    attribute.id ??= ids(ID_PREFIX.attribute, `${scope}/${name}`);
  }
}

/** A copy of the document in which every Kind, Relation and Attribute has an id. */
export function withIds(document: SchemaDocument, ids: IdFactory): SchemaDocument {
  const copy = structuredClone(document);
  for (const [name, kind] of Object.entries(copy.kinds ?? {})) {
    kind.id ??= ids(ID_PREFIX.kind, `kinds/${name}`);
    fillAttributeIds(kind.attributes, `kinds/${name}`, ids);
  }
  for (const [name, relation] of Object.entries(copy.relations ?? {})) {
    relation.id ??= ids(ID_PREFIX.relation, `relations/${name}`);
    fillAttributeIds(relation.attributes, `relations/${name}`, ids);
  }
  return copy;
}

/** The Kind a type points at: `ref(Supplier)` gives `Supplier`. */
export function refTarget(type: TypeDocument | undefined): string | undefined {
  return typeof type === 'string' ? /^ref\((.+)\)$/.exec(type)?.[1] : undefined;
}

/** Every element a reviewer decides on: Kinds, their Attributes, Relations and link Attributes. */
export function elementIds(document: SchemaDocument): string[] {
  const out: string[] = [];
  for (const kind of Object.values(document.kinds ?? {})) {
    out.push(idOf(kind), ...Object.values(kind.attributes ?? {}).map(idOf));
  }
  for (const relation of Object.values(document.relations ?? {})) {
    out.push(idOf(relation), ...Object.values(relation.attributes ?? {}).map(idOf));
  }
  return out;
}

export interface DecisionCounts {
  total: number;
  accepted: number;
  dropped: number;
  pending: number;
}

export function countDecisions(document: SchemaDocument, decisions: Decisions): DecisionCounts {
  const all = elementIds(document);
  const accepted = all.filter((id) => decisions[id] === 'accepted').length;
  const dropped = all.filter((id) => decisions[id] === 'dropped').length;
  return { total: all.length, accepted, dropped, pending: all.length - accepted - dropped };
}

// ----------------------------------------------------------------- decisions

/** Sets (or, with `null`, clears) the decision of one element. */
export function decide(decisions: Decisions, id: string, value: Decision | null): Decisions {
  const rest = Object.entries(decisions).filter(([key]) => key !== id);
  return Object.fromEntries(value === null ? rest : [...rest, [id, value]]);
}

/** Deciding on a Kind. Accepting it also accepts the Attributes nobody has decided on yet. */
export function decideKind(
  decisions: Decisions,
  kind: KindDocument,
  value: Decision | null,
): Decisions {
  let next = decide(decisions, idOf(kind), value);
  if (value === 'accepted') {
    for (const attribute of Object.values(kind.attributes ?? {})) {
      if (next[idOf(attribute)] === undefined) next = decide(next, idOf(attribute), 'accepted');
    }
  }
  return next;
}

/** Deciding on a Relation. Accepting it also accepts its undecided link Attributes. */
export function decideRelation(
  decisions: Decisions,
  relation: RelationDocument,
  value: Decision | null,
): Decisions {
  let next = decide(decisions, idOf(relation), value);
  if (value === 'accepted') {
    for (const attribute of Object.values(relation.attributes ?? {})) {
      if (next[idOf(attribute)] === undefined) next = decide(next, idOf(attribute), 'accepted');
    }
  }
  return next;
}

/** Accepts every element whose confidence reaches the threshold, except the dropped ones. */
export function acceptAtOrAbove(
  document: SchemaDocument,
  evidence: EvidenceIndex,
  decisions: Decisions,
  threshold: number,
): Decisions {
  const next: Record<string, Decision> = { ...decisions };
  for (const id of elementIds(document)) {
    if (next[id] === 'dropped') continue;
    const confidence = evidence.get(id)?.confidence;
    if (confidence !== undefined && confidence + EPSILON >= threshold) next[id] = 'accepted';
  }
  return next;
}

// ------------------------------------------------------------------ analysis

export interface Analysis {
  /** The accepted elements that can stand together, as a schema document. */
  document: SchemaDocument;
  /** Accepted elements that are left out, with the reason. */
  notApplied: ReadonlyMap<string, string>;
  applied: { kinds: number; attributes: number; relations: number };
}

function remainingAttributes(
  attributes: Record<string, AttributeDocument> | undefined,
  decisions: Decisions,
  alwaysKept: ReadonlySet<string>,
) {
  return Object.fromEntries(
    Object.entries(attributes ?? {})
      .filter(([name, a]) => alwaysKept.has(name) || decisions[idOf(a)] === 'accepted')
      .map(([name, a]) => [name, structuredClone(a)]),
  );
}

/**
 * What the decisions add up to. A Kind needs to be accepted for its Attributes to count (its
 * key Attributes come with it); a Relation needs both Kinds and its reference Attribute; an
 * Attribute that references a Kind that is not accepted stays out. The rest is the reviewer's.
 */
export function analyze(document: SchemaDocument, decisions: Decisions): Analysis {
  const notApplied = new Map<string, string>();
  const kinds: Record<string, KindDocument> = {};
  const relations: Record<string, RelationDocument> = {};
  const accepted = new Set(
    Object.entries(document.kinds ?? {})
      .filter(([, kind]) => decisions[idOf(kind)] === 'accepted')
      .map(([name]) => name),
  );

  for (const [name, kind] of Object.entries(document.kinds ?? {})) {
    if (!accepted.has(name)) {
      for (const a of Object.values(kind.attributes ?? {})) {
        if (decisions[idOf(a)] === 'accepted') notApplied.set(idOf(a), `${name} is not accepted`);
      }
      continue;
    }
    const attributes = remainingAttributes(kind.attributes, decisions, new Set(kind.primary_key));
    for (const [attributeName, attribute] of Object.entries(attributes)) {
      const target = refTarget(attribute.type);
      if (target !== undefined && !accepted.has(target)) {
        notApplied.set(idOf(attribute), `${target} is not accepted`);
        delete attributes[attributeName];
      }
    }
    const next: KindDocument = { ...structuredClone(kind), attributes };
    const title = kind.title_attribute;
    if (title !== undefined && attributes[title] === undefined) {
      const fallback = Object.keys(attributes).find((n) => attributes[n]?.type === 'string');
      if (fallback === undefined) delete next.title_attribute;
      else next.title_attribute = fallback;
    }
    kinds[name] = next;
  }

  for (const [name, relation] of Object.entries(document.relations ?? {})) {
    const id = idOf(relation);
    if (decisions[id] !== 'accepted') continue;
    const missing = [relation.from_kind, relation.to_kind].find((k) => !accepted.has(k));
    if (missing !== undefined) {
      notApplied.set(id, `${missing} is not accepted`);
      continue;
    }
    const reference = relation.foreign_key_attribute;
    if (
      reference !== undefined &&
      kinds[relation.from_kind]?.attributes?.[reference] === undefined
    ) {
      notApplied.set(id, `its reference Attribute ${reference} is left out`);
      continue;
    }
    relations[name] = {
      ...structuredClone(relation),
      ...(relation.attributes === undefined
        ? {}
        : { attributes: remainingAttributes(relation.attributes, decisions, new Set()) }),
    };
  }

  const out: SchemaDocument = {
    ...(document.format === undefined ? {} : { format: document.format }),
    ...(document.domain === undefined ? {} : { domain: document.domain }),
    kinds,
    relations,
  };
  return {
    document: out,
    notApplied,
    applied: {
      kinds: Object.keys(kinds).length,
      attributes:
        Object.values(kinds).reduce((n, k) => n + Object.keys(k.attributes ?? {}).length, 0) +
        Object.values(relations).reduce((n, r) => n + Object.keys(r.attributes ?? {}).length, 0),
      relations: Object.keys(relations).length,
    },
  };
}

// --------------------------------------------------------------------- merge

/**
 * The proposal laid over the schema of the Branch, by api name. A Kind that exists keeps its
 * key, title and other settings and takes the proposed Attributes (an Attribute that exists
 * keeps its id, so the change set updates it); a Relation that exists stays as it is.
 */
export function mergeIntoBase(base: SchemaDocument, proposal: SchemaDocument): SchemaDocument {
  const merged = structuredClone(base);
  merged.kinds ??= {};
  merged.relations ??= {};
  for (const [name, kind] of Object.entries(proposal.kinds ?? {})) {
    const existing = merged.kinds[name];
    if (existing === undefined) {
      merged.kinds[name] = structuredClone(kind);
      continue;
    }
    existing.attributes ??= {};
    for (const [attributeName, attribute] of Object.entries(kind.attributes ?? {})) {
      const id = existing.attributes[attributeName]?.id ?? attribute.id;
      existing.attributes[attributeName] = { ...structuredClone(attribute), id };
    }
  }
  for (const [name, relation] of Object.entries(proposal.relations ?? {})) {
    merged.relations[name] ??= structuredClone(relation);
  }
  return merged;
}

/** Api names the proposal shares with the schema of the Branch. */
export function sharedNames(base: SchemaDocument, document: SchemaDocument) {
  const among = (own: object | undefined, other: object | undefined) =>
    new Set(Object.keys(own ?? {}).filter((name) => name in (other ?? {})));
  return {
    kinds: among(document.kinds, base.kinds),
    relations: among(document.relations, base.relations),
  };
}

// --------------------------------------------------------------------- edits

/** `to` is free: not empty and not used by a sibling. */
export function nameIsFree(names: readonly string[], from: string, to: string): boolean {
  return to !== '' && (to === from || !names.includes(to));
}

export function renameRelation(draft: SchemaDocument, from: string, to: string) {
  if (draft.relations === undefined || from === to) return;
  draft.relations = renameKey(draft.relations, from, to);
}

export function renameLinkAttribute(
  draft: SchemaDocument,
  relation: string,
  from: string,
  to: string,
) {
  const owner = draft.relations?.[relation];
  if (owner?.attributes === undefined || from === to) return;
  owner.attributes = renameKey(owner.attributes, from, to);
}

// ------------------------------------------------------------------ evidence

/**
 * Inclusion ratios the engine reports in the reasons of a reference ("100% of 22 distinct
 * values are keys of supplier(supplier_code)"): one for a foreign key, two for a junction.
 */
export function inclusionRatios(reasons: readonly string[]): number[] {
  return reasons.flatMap((reason) => {
    const found = /^(\d+(?:\.\d+)?)% of \d+ distinct values are keys of /.exec(reason);
    return found?.[1] === undefined ? [] : [Number(found[1]) / 100];
  });
}

/** Distinct sample values of an Attribute, to seed the values of an enum. */
export function enumSeed(evidence: InferenceEvidence | undefined): string[] {
  return [...new Set(evidence?.samples ?? [])];
}
