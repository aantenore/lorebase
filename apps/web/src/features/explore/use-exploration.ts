import { useCallback, useEffect, useRef, useState } from 'react';
import { explore as exploreMessages } from '@/messages/explore';
import type { ExploreEvent } from './events';
import { GuideError, type ExploreRequest, type ExploreTransport } from './explore-client';

export type StepEvent = Extract<
  ExploreEvent,
  { type: 'thought' | 'tool_call' | 'tool_result' | 'error' }
>;
export type AnswerEvent = Extract<ExploreEvent, { type: 'answer' }>;
export type DoneEvent = Extract<ExploreEvent, { type: 'done' }>;

export type ExploreStatus = 'idle' | 'running' | 'done' | 'stopped' | 'failed';

export interface ExplorationState {
  status: ExploreStatus;
  steps: StepEvent[];
  sessionId: string | null;
  /** Model name, when Guide reports one. */
  model: string | null;
  answer: AnswerEvent | null;
  done: DoneEvent | null;
  /** Why the exploration failed; `retryAt` (epoch ms) after a 429. */
  failure: { message: string; retryAt: number | null } | null;
}

const IDLE: ExplorationState = {
  status: 'idle',
  steps: [],
  sessionId: null,
  model: null,
  answer: null,
  done: null,
  failure: null,
};

function apply(state: ExplorationState, event: ExploreEvent): ExplorationState {
  switch (event.type) {
    case 'session':
      return { ...state, sessionId: event.session_id || null, model: event.model ?? state.model };
    case 'answer':
      return { ...state, answer: event };
    case 'done':
      return {
        ...state,
        done: event,
        sessionId: state.sessionId ?? (event.session_id || null),
        model: event.model ?? state.model,
      };
    default:
      return { ...state, steps: [...state.steps, event] };
  }
}

const isAbort = (error: unknown) => error instanceof DOMException && error.name === 'AbortError';

/**
 * One exploration at a time: `start` streams it into the state as events arrive, `stop` aborts
 * it (the request is cancelled, so Guide stops working on it). A run longer than `timeoutMs`
 * is stopped too. Unmounting aborts a running exploration.
 */
export function useExploration(transport: ExploreTransport | null, timeoutMs: number) {
  const [state, setState] = useState<ExplorationState>(IDLE);
  const controller = useRef<AbortController | null>(null);

  useEffect(
    () => () => {
      controller.current?.abort();
    },
    [],
  );

  const stop = useCallback(() => {
    controller.current?.abort();
  }, []);

  const start = useCallback(
    async (request: ExploreRequest) => {
      if (transport === null) return;
      controller.current?.abort();
      const own = new AbortController();
      controller.current = own;
      let timedOut = false;
      const timer = setTimeout(() => {
        timedOut = true;
        own.abort();
      }, timeoutMs);
      const update = (next: (current: ExplorationState) => ExplorationState) => {
        if (controller.current === own) setState(next);
      };
      setState({ ...IDLE, status: 'running' });
      try {
        for await (const event of transport(request, own.signal)) {
          update((current) => apply(current, event));
        }
        update((current) =>
          current.answer === null
            ? {
                ...current,
                status: 'failed',
                failure: { message: exploreMessages.errors.streamEnded, retryAt: null },
              }
            : { ...current, status: 'done' },
        );
      } catch (error) {
        if (isAbort(error) || own.signal.aborted) {
          update((current) =>
            timedOut
              ? {
                  ...current,
                  status: 'failed',
                  failure: {
                    message: `Stopped after ${Math.round(timeoutMs / 1000).toString()} s without an answer.`,
                    retryAt: null,
                  },
                }
              : { ...current, status: 'stopped' },
          );
        } else {
          const retryAt =
            error instanceof GuideError && error.retryAfterS !== null
              ? Date.now() + error.retryAfterS * 1000
              : null;
          const message =
            error instanceof GuideError ? error.message : exploreMessages.errors.unexpected;
          update((current) => ({ ...current, status: 'failed', failure: { message, retryAt } }));
        }
      } finally {
        clearTimeout(timer);
        if (controller.current === own) controller.current = null;
      }
    },
    [transport, timeoutMs],
  );

  return { state, start, stop };
}
