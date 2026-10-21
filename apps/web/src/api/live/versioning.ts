/**
 * Branches, Revisions, Proposals, Conflicts and Operation executions: Keeper responses as the
 * view models of the screens.
 */
import type { components } from '../generated/keeper';
import { refOf } from '../refs';
import type {
  Branch,
  ExecuteOperationResponse,
  MergeAttempt,
  MergeOutcome,
  OperationEdit,
  Resolution,
  Revision,
  RevisionOriginType,
  SchemaChangeResult,
  SchemaValidation,
  Violation,
} from '../types';

type S = components['schemas'];

export function mapBranch(detail: S['BranchDetail']): Branch {
  return {
    name: detail.name,
    head: detail.head,
    kind: 'branch',
    protected: detail.protected,
    behind_default: detail.behind,
    ahead_default: detail.ahead,
  };
}

export function mapTag(tag: S['TagOut']): Branch {
  return {
    name: tag.name,
    head: tag.revision,
    kind: 'tag',
    protected: true,
    behind_default: 0,
    ahead_default: 0,
  };
}

export function mapRevision(out: S['RevisionOut'], domain: string): Revision {
  const origin = out.origin as { type?: string; ref?: string | null };
  const stats = out.stats as { partitions_changed?: number; changes?: number };
  return {
    id: out.id,
    parents: out.parents,
    root: out.root,
    domain,
    author: { principal: out.author, on_behalf_of: out.on_behalf_of ?? null },
    message: out.message,
    origin: {
      type: (origin.type ?? 'operation') as RevisionOriginType,
      ref: origin.ref ?? null,
    },
    created_at: out.created_at,
    stats: { partitions_changed: stats.partitions_changed ?? 0, changes: stats.changes ?? 0 },
  };
}

// ------------------------------------------------------------ merge attempts

/**
 * A merge attempt as Keeper reports it. The Conflict, Resolution and attempt members of the
 * view are the ones Keeper sends (the view was written against them), so the mapping keeps the
 * object and only settles the members Keeper may leave null.
 */
export function mapAttempt(out: S['ConflictsOut']): MergeAttempt {
  return out as unknown as MergeAttempt;
}

export function mapResolution(out: S['ResolutionOut']): Resolution {
  return out as unknown as Resolution;
}

export function mapMerge(out: S['MergeOut']): MergeOutcome {
  return {
    status: 'merged',
    proposal: out.proposal,
    revision: out.revision,
    result: out.result,
    attempt: mapAttempt(out.attempt),
    source_deleted: out.source_deleted,
    replayed: out.replayed,
  };
}

// ------------------------------------------------------------------ findings

interface Finding {
  code?: string;
  message?: string;
  severity?: string;
  path?: (string | number)[];
  key?: string;
}

export function mapFinding(raw: Finding): Violation {
  return {
    code: raw.code ?? 'unknown',
    message: raw.message ?? '',
    ...(raw.path === undefined ? {} : { path: raw.path.join('/') }),
    ...(raw.key === undefined ? {} : { ref: raw.key }),
    ...(raw.severity === undefined ? {} : { severity: raw.severity }),
  };
}

export function mapFindings(findings: readonly Finding[] | undefined): Violation[] {
  return (findings ?? []).map(mapFinding);
}

const errorsOf = (violations: Violation[]) => violations.filter((v) => v.severity !== 'warning');

export function mapChangeResult(out: S['SchemaChangeOut']): SchemaChangeResult {
  const violations = mapFindings(out.issues);
  return {
    valid: errorsOf(violations).length === 0,
    revision: out.revision,
    violations,
    migrations:
      out.data_changes > 0
        ? [
            {
              description: 'Records and Links rewritten by the change set',
              affected_values: out.data_changes,
              failing_values: 0,
            },
          ]
        : [],
    changes: out.changes,
    data_changes: out.data_changes,
  };
}

/** A change set Keeper refused (422): an answer for the screens, with the findings. */
export function refusedChange(issues: readonly Finding[] | undefined): SchemaChangeResult {
  return { valid: false, revision: null, violations: mapFindings(issues), migrations: [] };
}

export function mapValidation(out: S['SchemaValidationOut']): SchemaValidation {
  const violations = mapFindings(out.issues);
  return { valid: out.valid, violations, changes: out.changes };
}

// --------------------------------------------------------------- operations

function mapEdit(edit: Record<string, unknown>): OperationEdit {
  const op = typeof edit['op'] === 'string' ? edit['op'] : 'edit';
  const kind = edit['kind'];
  const key = edit['key'];
  const values = edit['values'];
  return {
    action: op,
    ...(typeof kind === 'string' && typeof key === 'string' ? { ref: refOf(kind, key) } : {}),
    ...(typeof values === 'object' && values !== null
      ? { values: values as Record<string, unknown> }
      : {}),
  };
}

export function mapExecution(out: S['ExecutionOut']): ExecuteOperationResponse {
  const violations = mapFindings(out.violations);
  return {
    status: out.status,
    edits: out.edits.map(mapEdit),
    violations,
    ...(out.revision === null ? {} : { revision: out.revision }),
  };
}

/** An Operation Keeper refused (422): `rejected`, with the edits it computed and the findings. */
export function refusedExecution(
  edits: readonly Record<string, unknown>[] | undefined,
  issues: readonly Finding[] | undefined,
): ExecuteOperationResponse {
  return {
    status: 'rejected',
    edits: (edits ?? []).map(mapEdit),
    violations: mapFindings(issues),
  };
}
