import Graph from 'graphology';
import { generatePerfGraph } from '@/api/mock/perf-graph';
import { loadConfig } from '@/config/env';
import { LayoutController, movement, type LayoutRunner } from './layout-controller';
import { renderDetail } from './render-detail';

const policy = { timeBudgetMs: 10_000, sampleMs: 100, convergence: 0.01, stableSamples: 3 };

function setup() {
  const graph = new Graph();
  graph.addNode('a', { x: 0, y: 0 });
  graph.addNode('b', { x: 10, y: 10 });
  const runner = { start: vi.fn(), stop: vi.fn(), kill: vi.fn() } satisfies LayoutRunner;
  const controller = new LayoutController(graph, runner, policy);
  /** Moves node b by `dx` and lets one sample pass. */
  const step = (dx: number) => {
    graph.updateNodeAttribute('b', 'x', (x: number) => x + dx);
    vi.advanceTimersByTime(policy.sampleMs);
  };
  /** Keeps node b moving, as a layout that has not converged does. */
  const drift = () => {
    let out = false;
    return setInterval(() => {
      out = !out;
      graph.setNodeAttribute('b', 'x', out ? 15 : 10);
    }, policy.sampleMs);
  };
  return { controller, runner, step, drift };
}

describe('LayoutController', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('starts once and ignores a second start while running', () => {
    const { controller, runner } = setup();
    controller.start();
    controller.start();
    expect(runner.start).toHaveBeenCalledTimes(1);
    expect(controller.status).toBe('running');
  });

  it('settles after enough calm samples and stops the engine', () => {
    const { controller, runner, step } = setup();
    const seen: string[] = [];
    controller.subscribe((status) => seen.push(status));
    controller.start();
    step(5);
    step(0.01);
    step(0.01);
    expect(controller.status).toBe('running');
    step(0.01);
    expect(controller.status).toBe('settled');
    expect(runner.stop).toHaveBeenCalledTimes(1);
    expect(seen).toEqual(['running', 'settled']);
  });

  it('restarts the calm count when the nodes move again', () => {
    const { controller, step } = setup();
    controller.start();
    step(0.01);
    step(0.01);
    step(5);
    step(0.01);
    step(0.01);
    expect(controller.status).toBe('running');
  });

  it('stops at the time budget even while nodes still move', () => {
    const { controller, runner, drift } = setup();
    drift();
    controller.start();
    vi.advanceTimersByTime(policy.timeBudgetMs);
    expect(controller.status).toBe('timeout');
    expect(runner.stop).toHaveBeenCalledTimes(1);
  });

  it('pauses and resumes with a fresh budget', () => {
    const { controller, runner, drift } = setup();
    drift();
    controller.start();
    vi.advanceTimersByTime(policy.timeBudgetMs - 1);
    controller.stop();
    expect(controller.status).toBe('stopped');
    controller.start();
    vi.advanceTimersByTime(policy.timeBudgetMs - 1);
    expect(controller.status).toBe('running');
    expect(runner.start).toHaveBeenCalledTimes(2);
  });

  it('kills the engine on dispose and cannot start again', () => {
    const { controller, runner } = setup();
    controller.start();
    controller.dispose();
    controller.start();
    expect(runner.kill).toHaveBeenCalledTimes(1);
    expect(runner.start).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(policy.timeBudgetMs);
    expect(runner.stop).not.toHaveBeenCalled();
  });
});

describe('movement', () => {
  it('is the mean move over the extent', () => {
    const before = new Float64Array([0, 0, 10, 0]);
    const after = new Float64Array([0, 0, 10, 1]);
    expect(movement(before, after)).toBeCloseTo(0.05);
  });
});

describe('renderDetail', () => {
  const { detail } = loadConfig({ VITE_API_MODE: 'mock' }).canvas;

  it('draws everything on a small graph', () => {
    expect(renderDetail(20, 30, detail)).toEqual({});
  });

  it('thins labels and edges on a large graph', () => {
    expect(renderDetail(5000, 10_000, detail)).toEqual({
      renderEdgeLabels: false,
      hideEdgesOnMove: true,
      labelRenderedSizeThreshold: detail.denseLabelMinSize,
    });
  });
});

describe('generatePerfGraph', () => {
  it('is deterministic and reaches the requested size', () => {
    const a = generatePerfGraph(500, 1000, ['K1', 'K2']);
    expect(a).toEqual(generatePerfGraph(500, 1000, ['K1', 'K2']));
    expect(a.nodes).toHaveLength(500);
    expect(a.edges).toHaveLength(1000);
    const refs = new Set(a.nodes.map((n) => n.ref));
    expect(a.edges.every((e) => refs.has(e.from) && refs.has(e.to))).toBe(true);
  });
});
