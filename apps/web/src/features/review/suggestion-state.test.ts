import type { SchemaSuggestion } from '@/api/suggestion-types';
import {
  DEFAULT_SUGGESTION_FILTER,
  isDecidable,
  ownerLabel,
  refLabel,
  toDecideInput,
  toSuggestionFilter,
} from './suggestion-state';

const suggestion = (patch: Partial<SchemaSuggestion>): SchemaSuggestion => ({
  id: 'sug_1',
  type: 'kind',
  name: 'Notice',
  kind: null,
  target: null,
  labels: [],
  support: 3,
  documents: 1,
  confidence: 0.5,
  status: 'pending',
  type_guess: null,
  samples: [],
  examples: [],
  sources: [],
  duplicate_of: null,
  withheld: false,
  proposal_id: null,
  branch: null,
  decided_by: null,
  decided_at: null,
  created_at: null,
  updated_at: null,
  ...patch,
});

describe('suggestion filter', () => {
  it('lists pending suggestions at or above the minimum support by default', () => {
    expect(toSuggestionFilter(DEFAULT_SUGGESTION_FILTER)).toEqual({ status: ['pending'] });
  });

  it('sends every restriction that is set', () => {
    expect(toSuggestionFilter({ status: 'all', type: 'relation', belowMinSupport: true })).toEqual({
      type: ['relation'],
      all: true,
    });
  });
});

describe('suggestion labels', () => {
  it('marks a Kind another suggestion proposes', () => {
    expect(refLabel('+Notice')).toBe('Notice (new)');
    expect(refLabel('Part')).toBe('Part');
  });

  it('names the owner of an Attribute and the ends of a Relation', () => {
    expect(ownerLabel(suggestion({}))).toBe('');
    expect(ownerLabel(suggestion({ type: 'kind_alias', kind: 'Supplier' }))).toBe('Supplier');
    expect(
      ownerLabel(
        suggestion({
          type: 'value_alias',
          kind: 'Supplier',
          attribute: 'status',
          value: 'on_hold',
        }),
      ),
    ).toBe('Supplier.status');
    expect(ownerLabel(suggestion({ type: 'attribute', kind: 'Supplier' }))).toBe('Supplier');
    expect(ownerLabel(suggestion({ type: 'relation', kind: '+Notice', target: 'Part' }))).toBe(
      'Notice (new) to Part',
    );
  });
});

describe('toDecideInput', () => {
  const rows = [suggestion({ id: 'a', name: 'ends' }), suggestion({ id: 'b', name: 'part' })];

  it('renames accepted suggestions only when the name differs', () => {
    expect(toDecideInput(rows, 'accepted', { a: ' valid_until ', b: 'part' })).toEqual({
      decisions: [
        { suggestion: 'a', decision: 'accepted', name: 'valid_until' },
        { suggestion: 'b', decision: 'accepted' },
      ],
    });
  });

  it('drops renames from rejections and resets', () => {
    expect(toDecideInput(rows, 'rejected', { a: 'x' }).decisions[0]).toEqual({
      suggestion: 'a',
      decision: 'rejected',
    });
  });
});

describe('isDecidable', () => {
  it('refuses accepted suggestions, which Keeper answers with a 409', () => {
    expect(isDecidable(suggestion({ status: 'accepted' }))).toBe(false);
    expect(isDecidable(suggestion({ status: 'rejected' }))).toBe(true);
  });
});
