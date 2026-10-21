// @vitest-environment node
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import openapiTS, { astToString, COMMENT_HEADER } from 'openapi-typescript';
import { describe, expect, it } from 'vitest';

const OPENAPI = new URL('../../../../../docs/openapi.json', import.meta.url);
const GENERATED = new URL('./keeper.ts', import.meta.url);

describe('generated Keeper types', () => {
  it('match docs/openapi.json (run `pnpm types` after `make openapi`)', async () => {
    const expected = COMMENT_HEADER + astToString(await openapiTS(OPENAPI));
    const actual = readFileSync(fileURLToPath(GENERATED), 'utf8');
    // The CLI writes a trailing newline after the printed AST.
    expect(actual.trimEnd()).toBe(expected.trimEnd());
  });
});
