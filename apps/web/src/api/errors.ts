import type { ProblemDetails } from './types';

/** An RFC 9457 problem returned by Keeper (or produced by the mock adapter). */
export class ApiError extends Error {
  readonly problem: ProblemDetails;

  constructor(problem: ProblemDetails) {
    super(problem.detail ?? problem.title);
    this.name = 'ApiError';
    this.problem = problem;
  }

  get status(): number {
    return this.problem.status;
  }

  get code(): string | undefined {
    return this.problem.code;
  }
}

export function problem(status: number, title: string, detail?: string, code?: string): ApiError {
  return new ApiError({
    type: 'about:blank',
    title,
    status,
    ...(detail === undefined ? {} : { detail }),
    ...(code === undefined ? {} : { code }),
  });
}

export function notFound(what: string): ApiError {
  return problem(404, 'Not found', `${what} does not exist.`, 'not_found');
}

export function notSupported(what: string): ApiError {
  return problem(501, 'Not supported', `${what} is not available in mock mode.`, 'not_supported');
}

/** HTTP 409: the request clashes with the current state (for example unresolved Conflicts). */
export function stateError(detail: string, code?: string): ApiError {
  return problem(409, 'State mismatch', detail, code);
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}
