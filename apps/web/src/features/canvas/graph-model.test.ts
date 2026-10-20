import { buildGraph, fromSchema, type GraphData } from './graph-model';
import { buildSampleSeed } from '@/api/mock/adapter';

describe('graph model', () => {
  const data: GraphData = {
    nodes: [
      { id: 'a', label: 'A', group: 'K', color: '#111111' },
      { id: 'b', label: 'B', group: 'K', color: '#111111' },
      { id: 'c', label: 'C', group: 'K', color: '#111111' },
    ],
    edges: [
      { id: 'e1', source: 'a', target: 'b', label: 'r' },
      { id: 'e2', source: 'a', target: 'c', label: 'r' },
      { id: 'e3', source: 'a', target: 'missing', label: 'r' },
    ],
  };

  it('sizes nodes by degree and positions them', () => {
    const graph = buildGraph(data, 20);
    expect(graph.order).toBe(3);
    expect(graph.size).toBe(2);
    const hub = graph.getNodeAttribute('a', 'size') as number;
    const leaf = graph.getNodeAttribute('b', 'size') as number;
    expect(hub).toBeGreaterThan(leaf);
    expect(Number.isFinite(graph.getNodeAttribute('a', 'x'))).toBe(true);
  });

  it('builds the schema graph from Kinds and Relations', () => {
    const supply = buildSampleSeed().domains[0];
    const main = supply?.branches.get('main');
    if (main === undefined) throw new Error('sample missing');
    const schema = { domain: 'supply', branch: 'main', revision: 'r', ...main.schema };
    const graph = fromSchema(schema);
    expect(graph.nodes.map((n) => n.id)).toEqual(['Supplier', 'Part', 'Plant', 'Contract']);
    expect(graph.edges.map((e) => e.label)).toContain('supplied_by (many_to_many)');
  });
});
