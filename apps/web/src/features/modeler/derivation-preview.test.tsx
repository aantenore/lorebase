import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { KeeperApi } from '@/api';
import { ValidationError } from '@/api/errors';
import type { PreviewError } from '@/api/preview-types';
import type { JsonValue } from '@/api/types';
import { asRemote, createMockApi } from '@/api/mock/adapter';
import { testConfig } from '@/config/env';
import { renderApp } from '@/test/render';

const options = { pageSize: 50, searchLimit: 20, maxNodes: 200 };

async function openDerivations(api?: KeeperApi) {
  const user = userEvent.setup();
  renderApp('/supply/modeler?view=edit&branch=model%2Frisk', testConfig(), api);
  await screen.findByRole('textbox', { name: 'Schema YAML' });
  await user.click(screen.getByRole('tab', { name: /^Derivations/ }));
  return user;
}

describe('Derivation preview', () => {
  it('shows the computed Attribute on sample Records of the saved Derivation', async () => {
    await openDerivations();
    const table = await screen.findByRole('table', { name: /^Sample values of / });
    expect(within(table).getAllByRole('row').length).toBeGreaterThan(1);
    expect(within(table).getAllByRole('row').length).toBeLessThanOrEqual(6);
  });

  it('evaluates an edited Derivation through Keeper without saving it', async () => {
    const user = await openDerivations();
    await screen.findByRole('table', { name: /^Sample values of / });
    const description = screen.getByRole('textbox', { name: 'Derivation description' });
    await user.type(description, ' edited');
    // The saved values give way to the button: a result never outlives the text it came from.
    expect(screen.queryByRole('table', { name: /^Sample values of / })).toBeNull();
    await user.click(await screen.findByRole('button', { name: 'Preview the edited Derivation' }));
    const table = await screen.findByRole('table', { name: /^Sample values of / });
    expect(within(table).getAllByRole('row').length).toBeGreaterThan(1);
    await user.type(description, ' again');
    expect(screen.queryByRole('table', { name: /^Sample values of / })).toBeNull();
  });

  it("shows Keeper's masked values and errors, and the issues of an edit it refuses", async () => {
    const mock = asRemote(createMockApi(options), 0);
    const item = (
      key: string,
      value: JsonValue,
      masked: boolean,
      error: PreviewError | null = null,
    ) => ({
      key,
      title: `Part ${key}`,
      value,
      masked,
      error,
    });
    const answers: (() => ReturnType<KeeperApi['derivations']['preview']>)[] = [
      () =>
        Promise.resolve({
          revision: 'r1',
          kind: 'Part',
          attribute: 'single_sourced',
          items: [
            item('P-1', '***', true),
            item('P-2', null, false, {
              code: 'expression.evaluation',
              message: 'division by zero',
            }),
            item('P-3', null, true, { code: null, message: null }),
          ],
        }),
      () =>
        Promise.reject(
          new ValidationError({
            type: 'about:blank',
            title: 'Request validation failed',
            status: 422,
            detail: '1 validation issue(s); nothing was written.',
            code: 'validation-failed',
            issues: [
              {
                code: 'schema.expression_invalid',
                message: 'cannot compare int with string',
                path: ['derivations', 'single_sourced'],
              },
            ],
          }),
        ),
    ];
    const api: KeeperApi = {
      ...mock,
      derivations: {
        preview: () => answers.shift()?.() ?? Promise.reject(new Error('No answer left.')),
      },
    };
    const user = await openDerivations(api);
    await user.type(screen.getByRole('textbox', { name: 'Derivation description' }), ' edited');
    await user.click(await screen.findByRole('button', { name: 'Preview the edited Derivation' }));
    const table = await screen.findByRole('table', { name: /^Sample values of / });
    const rows = within(table).getAllByRole('row');
    expect(within(rows[1] as HTMLElement).getByText('***')).toBeInTheDocument();
    expect(within(rows[1] as HTMLElement).getByText('masked')).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByText('division by zero')).toBeInTheDocument();
    expect(within(rows[3] as HTMLElement).getByText('Cannot be computed.')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Preview the edited Derivation' }));
    expect(
      await screen.findByText('derivations/single_sourced: cannot compare int with string'),
    ).toBeInTheDocument();
  });
});
