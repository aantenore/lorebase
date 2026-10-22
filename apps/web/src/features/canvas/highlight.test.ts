import {
  DIMMED_EDGE_OPACITY,
  DIMMED_NODE_OPACITY,
  edgeEmphasis,
  nodeEmphasis,
  toSets,
} from './highlight';

describe('highlight', () => {
  it('leaves the graph as it is when nothing is highlighted', () => {
    expect(nodeEmphasis(null, 'a')).toEqual({});
    expect(edgeEmphasis(null, 'e', 1)).toEqual({});
  });

  it('accepts arrays and sets', () => {
    expect(toSets({ nodes: ['a', 'b'] }).nodes).toEqual(new Set(['a', 'b']));
    expect(toSets({ nodes: new Set(['a']), edges: ['e'] }).edges).toEqual(new Set(['e']));
    expect(toSets({ nodes: [] }).edges.size).toBe(0);
  });

  it('dims what is not highlighted and lifts what is', () => {
    const sets = toSets({ nodes: ['a'], edges: ['e1'] });
    expect(nodeEmphasis(sets, 'a')).toEqual({ zIndex: 1, labelVisibility: 'visible' });
    expect(nodeEmphasis(sets, 'b')).toEqual({
      opacity: DIMMED_NODE_OPACITY,
      labelVisibility: 'hidden',
    });
    expect(edgeEmphasis(sets, 'e1', 2)).toMatchObject({ zIndex: 1, size: 5 });
    expect(edgeEmphasis(sets, 'e2', 2)).toEqual({ opacity: DIMMED_EDGE_OPACITY });
  });
});
