import type { AttributeType } from '@/api/types';

function scalar(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint') {
    return String(value);
  }
  return JSON.stringify(value) ?? '';
}

/** Type-aware rendering of an Attribute value. */
export function ValueView({ value, type }: { value: unknown; type?: AttributeType }) {
  if (value === null || value === undefined) return <span className="text-muted">-</span>;
  if (typeof value === 'boolean') return <>{value ? 'Yes' : 'No'}</>;
  if (Array.isArray(value)) return <>{value.map(scalar).join(', ')}</>;
  if (typeof value === 'object') {
    return <code className="font-mono text-xs">{scalar(value)}</code>;
  }
  if (type === 'integer' || type === 'decimal' || type === 'float') {
    return <span className="tabular-nums">{scalar(value)}</span>;
  }
  if (type === 'uri') return <span className="break-all underline">{scalar(value)}</span>;
  return <>{scalar(value)}</>;
}
