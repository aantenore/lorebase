import type Graph from 'graphology';

/** idle: not started; settled: converged; timeout: stopped by the time budget; stopped: by a person. */
export type LayoutStatus = 'idle' | 'running' | 'settled' | 'timeout' | 'stopped';

/** What the controller needs from a layout engine; the ForceAtlas2 worker supervisor fits. */
export interface LayoutRunner {
  start: () => void;
  stop: () => void;
  kill: () => void;
}

export interface LayoutPolicy {
  /** Hard stop for a run, in milliseconds. */
  timeBudgetMs: number;
  /** Interval between two convergence samples, in milliseconds. */
  sampleMs: number;
  /** Mean node move per sample, as a fraction of the graph extent, under which a sample is calm. */
  convergence: number;
  /** Consecutive calm samples needed before the layout is considered settled. */
  stableSamples: number;
}

type Listener = (status: LayoutStatus) => void;

/**
 * Owns the life cycle of one layout run over one graph: start and stop, stopping by itself when
 * the nodes stop moving or the time budget is spent. It reads node positions, which the engine
 * writes into the graph, so it works with any engine behind `LayoutRunner`. It is created once
 * per graph and outlives React renders: only a new graph or `dispose` ends it.
 */
export class LayoutController {
  private current: LayoutStatus = 'idle';
  private readonly listeners = new Set<Listener>();
  private sampler: ReturnType<typeof setInterval> | null = null;
  private budget: ReturnType<typeof setTimeout> | null = null;
  private previous: Float64Array | null = null;
  private calm = 0;
  private disposed = false;

  constructor(
    private readonly graph: Graph,
    private readonly runner: LayoutRunner,
    private readonly policy: LayoutPolicy,
  ) {}

  get status(): LayoutStatus {
    return this.current;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /** Starts, or resumes with a fresh time budget. Does nothing while already running. */
  start(): void {
    if (this.disposed || this.current === 'running') return;
    this.calm = 0;
    this.previous = this.positions();
    this.runner.start();
    this.sampler = setInterval(() => {
      this.sample();
    }, this.policy.sampleMs);
    this.budget = setTimeout(() => {
      this.finish('timeout');
    }, this.policy.timeBudgetMs);
    this.set('running');
  }

  /** Stops on request; positions stay as they are. */
  stop(): void {
    this.finish('stopped');
  }

  /** Releases the engine and the timers; the controller cannot be started again. */
  dispose(): void {
    if (this.disposed) return;
    this.clearTimers();
    this.disposed = true;
    this.listeners.clear();
    this.runner.kill();
  }

  private set(status: LayoutStatus): void {
    this.current = status;
    for (const listener of [...this.listeners]) listener(status);
  }

  private finish(status: LayoutStatus): void {
    if (this.current !== 'running') return;
    this.clearTimers();
    this.runner.stop();
    this.set(status);
  }

  private clearTimers(): void {
    if (this.sampler !== null) clearInterval(this.sampler);
    if (this.budget !== null) clearTimeout(this.budget);
    this.sampler = null;
    this.budget = null;
  }

  private sample(): void {
    const now = this.positions();
    const before = this.previous;
    this.previous = now;
    if (before === null || before.length !== now.length || now.length === 0) return;
    this.calm = movement(before, now) < this.policy.convergence ? this.calm + 1 : 0;
    if (this.calm >= this.policy.stableSamples) this.finish('settled');
  }

  /** Interleaved x, y of every node, in graph order. */
  private positions(): Float64Array {
    const out = new Float64Array(this.graph.order * 2);
    let index = 0;
    this.graph.forEachNode((_node, attributes) => {
      out[index] = Number(attributes['x']);
      out[index + 1] = Number(attributes['y']);
      index += 2;
    });
    return out;
  }
}

/** Mean distance the nodes moved between two samples, as a fraction of the graph extent. */
export function movement(before: Float64Array, after: Float64Array): number {
  const count = after.length / 2;
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  let travelled = 0;
  for (let i = 0; i < after.length; i += 2) {
    const x = after[i] ?? 0;
    const y = after[i + 1] ?? 0;
    minX = Math.min(minX, x);
    maxX = Math.max(maxX, x);
    minY = Math.min(minY, y);
    maxY = Math.max(maxY, y);
    travelled += Math.hypot(x - (before[i] ?? 0), y - (before[i + 1] ?? 0));
  }
  const extent = Math.max(maxX - minX, maxY - minY, Number.EPSILON);
  return travelled / count / extent;
}
