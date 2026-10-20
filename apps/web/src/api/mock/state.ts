import type {
  AutoResolution,
  BranchName,
  Conflict,
  Domain,
  ProposalDetail,
  ProposalItem,
  RecordRef,
  Resolution,
  Revision,
  RevisionId,
  Schema,
} from '../types';

/** A stored Record: values keyed by Attribute api name. */
export interface StoredRecord {
  kind: string;
  key: string;
  values: Record<string, unknown>;
  derived: Record<string, unknown>;
}

export interface StoredLink {
  relation: string;
  from: RecordRef;
  to: RecordRef;
  values: Record<string, unknown>;
}

export type SchemaBody = Omit<Schema, 'domain' | 'branch' | 'revision'>;

/** Everything one Branch holds at its head. */
export interface BranchData {
  protected: boolean;
  schema: SchemaBody;
  records: StoredRecord[];
  links: StoredLink[];
  /** Linear history, head first. */
  log: Revision[];
}

export interface ProposalState {
  detail: ProposalDetail;
  /** The Conflicts of the merge attempt; empty when the merge is clean. */
  conflicts: Conflict[];
  /** Stored by fingerprint; a Conflict without an entry is unresolved. */
  resolutions: Resolution[];
  autoResolutions: AutoResolution[];
  /** Base, target and source heads, frozen once the merge Revision exists. */
  mergedHeads?: { base: RevisionId; ours: RevisionId; theirs: RevisionId };
  items: ProposalItem[];
}

export interface DomainData {
  domain: Domain;
  branches: Map<BranchName, BranchData>;
  proposals: Map<string, ProposalState>;
}

export interface MockSeed {
  domains: DomainData[];
}

export function headOf(data: BranchData): RevisionId {
  const head = data.log[0];
  if (head === undefined) throw new Error('A Branch must have at least one Revision.');
  return head.id;
}

export { refOf, splitRecordRef as splitRef } from '../refs';
