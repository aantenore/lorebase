import { useMutation } from '@tanstack/react-query';
import { useState } from 'react';
import { useKeeperApi } from '@/api/provider';
import { useScope } from '@/app/scope';
import { Button } from '@/ui/button';
import { Input } from '@/ui/field';
import { Badge } from '@/ui/layout';
import type { TypeDocument } from './document';
import { asText, isRecord, typeLabel } from './form-helpers';

/** A parameter value as typed: numbers and booleans for those types, text otherwise. */
export function parameterValue(type: string, text: string): unknown {
  if (type === 'integer' || type === 'decimal' || type === 'float') {
    const number = Number(text);
    return text.trim() === '' || Number.isNaN(number) ? text : number;
  }
  if (type === 'boolean') return text === 'true' ? true : text === 'false' ? false : text;
  if (type.startsWith('array<') || type === 'json') {
    try {
      return JSON.parse(text) as unknown;
    } catch {
      return text;
    }
  }
  return text;
}

/**
 * Dry run of a saved Operation on the current Branch (`dry_run: true`): the edits it would
 * make and the violations it would hit. Nothing is committed.
 */
export function OperationDryRun({ name, parameters }: { name: string; parameters: unknown[] }) {
  const api = useKeeperApi();
  const scope = useScope();
  const [values, setValues] = useState<Record<string, string>>({});
  const typed = parameters.filter(isRecord).map((p) => ({
    name: asText(p['name']),
    type: typeLabel(p['type'] as TypeDocument | undefined),
  }));
  const run = useMutation({
    mutationFn: () =>
      api.operations.execute(scope.domain, name, {
        branch: scope.branch,
        dry_run: true,
        parameters: Object.fromEntries(
          typed
            .filter((p) => (values[p.name] ?? '') !== '')
            .map((p) => [p.name, parameterValue(p.type, values[p.name] ?? '')]),
        ),
      }),
  });
  return (
    <div className="flex flex-col gap-3" role="group" aria-label={`Dry run of ${name}`}>
      <p className="text-xs text-muted">
        Runs the saved version of {name} on {scope.branch} without committing. Record references are
        keys; lists and JSON are written as JSON.
      </p>
      {typed.length > 0 && (
        <div className="grid gap-2 sm:grid-cols-2">
          {typed.map((parameter) => (
            <Input
              key={parameter.name}
              label={`Dry run value of ${parameter.name}`}
              hint={parameter.type}
              value={values[parameter.name] ?? ''}
              onChange={(event) => {
                const value = event.target.value;
                setValues((previous) => ({ ...previous, [parameter.name]: value }));
              }}
            />
          ))}
        </div>
      )}
      <div>
        <Button
          disabled={run.isPending}
          onClick={() => {
            run.mutate();
          }}
        >
          Dry run on {scope.branch}
        </Button>
      </div>
      {run.isError && (
        <p role="alert" className="text-sm text-danger">
          {run.error.message}
        </p>
      )}
      {run.data !== undefined && (
        <div role="status" className="flex flex-col gap-2 text-sm">
          <p>
            <Badge tone={run.data.violations.length === 0 ? 'success' : 'danger'}>
              {run.data.status}
            </Badge>{' '}
            {run.data.edits.length} edits, {run.data.violations.length} violations.
          </p>
          {run.data.edits.length > 0 && (
            <ul className="list-disc pl-5">
              {run.data.edits.map((edit, index) => (
                <li key={index}>
                  {edit.action}
                  {edit.ref === undefined ? '' : ` ${edit.ref}`}
                </li>
              ))}
            </ul>
          )}
          {run.data.violations.length > 0 && (
            <ul className="list-disc pl-5 text-danger">
              {run.data.violations.map((violation, index) => (
                <li key={index}>
                  {violation.code}: {violation.message}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
