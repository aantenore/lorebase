import type { components } from '../generated/keeper';
import { mapSuggestion, suggestionDecideBody, suggestionQuery } from './suggestions';

const wire: components['schemas']['SuggestionOut'] = {
  id: 'sug_1',
  type: 'attribute',
  name: 'ends',
  kind: 'Supplier',
  labels: [],
  support: 2,
  documents: 1,
  confidence: 0.5,
  status: 'pending',
  samples: [],
  examples: [
    { document_id: 'doc_1', chunk: 2, start: 4, end: 9, quote: 'ended' },
    { document_id: 'doc_1', quote: 'no offsets' },
  ],
  sources: [],
  withheld: false,
  proposal_id: null,
  branch: null,
  decided_by: null,
  decided_at: null,
  created_at: null,
  updated_at: null,
};

describe('suggestion mapping', () => {
  it('makes the optional members explicit and keeps well-formed evidence only', () => {
    const mapped = mapSuggestion(wire);
    expect(mapped.target).toBeNull();
    expect(mapped.type_guess).toBeNull();
    expect(mapped.duplicate_of).toBeNull();
    expect(mapped.examples).toEqual([
      { document_id: 'doc_1', chunk: 2, start: 4, end: 9, quote: 'ended' },
    ]);
  });

  it('sends one query parameter per value and leaves unset filters out', () => {
    expect(suggestionQuery({ status: ['pending', 'rejected'], type: [], all: false })).toEqual({
      status: { repeat: ['pending', 'rejected'] },
      type: undefined,
      all: undefined,
    });
    expect(suggestionQuery({ all: true }).all).toBe(true);
  });

  it('sends a name only when one is given', () => {
    expect(
      suggestionDecideBody({
        decisions: [
          { suggestion: 'a', decision: 'accepted', name: 'x' },
          { suggestion: 'b', decision: 'rejected' },
        ],
      }),
    ).toEqual({
      decisions: [
        { suggestion: 'a', decision: 'accepted', name: 'x' },
        { suggestion: 'b', decision: 'rejected' },
      ],
    });
  });
});
