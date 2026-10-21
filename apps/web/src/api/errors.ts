import type { MergeAttempt, ProblemDetails, ProblemIssue } from './types';

/** An RFC 9457 problem returned by Keeper (or produced by the mock adapter). */
export class ApiError extends Error {
  readonly problem: ProblemDetails;

  constructor(problem: ProblemDetails) {
    super(problem.detail ?? problem.title);
    this.name = new.target.name;
    this.problem = problem;
  }

  get status(): number {
    return this.problem.status;
  }

  get code(): string | undefined {
    return this.problem.code;
  }
}

/** The request never got an answer: offline, refused, blocked by CORS or interrupted. */
export class NetworkError extends ApiError {
  constructor(detail: string) {
    super({ type: 'about:blank', title: 'Keeper is unreachable', status: 0, detail });
  }
}

/** 401: no credential, or one Keeper does not accept. */
export class UnauthorizedError extends ApiError {}

/** 403: the credential lacks the permission the problem names. */
export class ForbiddenError extends ApiError {}

/** 404: Domain, Branch, Revision, Kind, Record, Proposal or element not found. */
export class NotFoundError extends ApiError {}

/** 422: the request was understood and refused; `issues` and `fields` say where. */
export class ValidationError extends ApiError {
  get issues(): readonly ProblemIssue[] {
    return this.problem.issues ?? [];
  }

  get fields(): readonly { pointer: string; message: string; code: string }[] {
    return this.problem.errors ?? [];
  }
}

/** 409: the request clashes with the current state. `code` tells which clash. */
export class ConflictError extends ApiError {}

/** 409 `protected-branch`: the Branch takes changes through a Proposal only. */
export class ProtectedBranchError extends ConflictError {}

/** 409 `approvals-required`: the target Branch needs more approvals before it takes a merge. */
export class ApprovalsRequiredError extends ConflictError {}

/** 409 `merge-conflicts`: the merge stopped on Conflicts; `attempt` holds them. */
export class MergeConflictsError extends ConflictError {
  get attempt(): MergeAttempt | undefined {
    return this.problem.attempt;
  }
}

/** 503 `branch-busy` and other temporary refusals; retry later. */
export class UnavailableError extends ApiError {}

/** 429. */
export class RateLimitedError extends ApiError {}

/** 5xx other than 503. */
export class ServerError extends ApiError {}

/** Problem codes that have a class of their own. */
export const PROBLEM_CODES = {
  protectedBranch: 'protected-branch',
  approvalsRequired: 'approvals-required',
  mergeConflicts: 'merge-conflicts',
  notSupported: 'not_supported',
} as const;

/** The typed error for a problem: by code where Keeper has one, otherwise by status. */
export function apiErrorFromProblem(problem: ProblemDetails): ApiError {
  switch (problem.code) {
    case PROBLEM_CODES.protectedBranch:
      return new ProtectedBranchError(problem);
    case PROBLEM_CODES.approvalsRequired:
      return new ApprovalsRequiredError(problem);
    case PROBLEM_CODES.mergeConflicts:
      return new MergeConflictsError(problem);
    default:
  }
  switch (problem.status) {
    case 401:
      return new UnauthorizedError(problem);
    case 403:
      return new ForbiddenError(problem);
    case 404:
      return new NotFoundError(problem);
    case 409:
      return new ConflictError(problem);
    case 422:
      return new ValidationError(problem);
    case 429:
      return new RateLimitedError(problem);
    case 503:
      return new UnavailableError(problem);
    default:
      return problem.status >= 500 ? new ServerError(problem) : new ApiError(problem);
  }
}

export function problem(status: number, title: string, detail?: string, code?: string): ApiError {
  return apiErrorFromProblem({
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
  return problem(
    501,
    'Not supported',
    `${what} is not available here.`,
    PROBLEM_CODES.notSupported,
  );
}

/** HTTP 409: the request clashes with the current state (for example unresolved Conflicts). */
export function stateError(detail: string, code?: string): ApiError {
  return problem(409, 'State mismatch', detail, code);
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

const HINTS: Partial<Record<number, string>> = {
  401: 'Set the API token in Settings.',
  403: 'Your credential does not allow this.',
};

/**
 * One line for a toast: what went wrong, and where Keeper pointed. Keeps the problem detail,
 * adds the first finding of a rejected request, and a hint for the cases a person can act on.
 */
export function describeError(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return error instanceof Error && error.message !== '' ? error.message : 'The request failed.';
  }
  if (error instanceof NetworkError)
    return `${error.problem.title}. ${error.problem.detail ?? ''}`.trim();
  const base = error.message;
  const finding =
    error instanceof ValidationError
      ? (error.issues[0]?.message ?? error.fields[0]?.message)
      : undefined;
  const extra = finding === undefined || base.includes(finding) ? '' : ` ${finding}`;
  const hint = HINTS[error.status];
  return `${base}${extra}${hint === undefined ? '' : ` ${hint}`}`.trim();
}
