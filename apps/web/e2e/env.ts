/**
 * Where the end-to-end run finds its pieces. Everything comes from environment variables, so
 * the same suite runs against a stack it starts itself, against a Keeper somebody started
 * (CI starts one from the repository), or on a machine whose default ports are taken.
 *
 *   LOREBASE_E2E_URL           Keeper to use; when set, no stack is started or stopped.
 *   LOREBASE_E2E_KEEPER_PORT   Host port of the Keeper of the compose stack.
 *   LOREBASE_E2E_POSTGRES_PORT Host port of its Postgres.
 *   LOREBASE_E2E_WEB_PORT      Port of the app under test (vite preview).
 *   LOREBASE_E2E_PROJECT       Compose project name.
 *   LOREBASE_E2E_KEEP          Leave the compose stack running after the run.
 */
import { fileURLToPath } from 'node:url';

const num = (name: string, fallback: number): number => {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 1 || value > 65535) {
    throw new Error(`${name} must be a port number, got "${raw}".`);
  }
  return value;
};

export interface E2eEnv {
  /** Keeper origin, without the API path. */
  keeperUrl: string;
  /** True when the Keeper was started by somebody else. */
  external: boolean;
  keeperPort: number;
  postgresPort: number;
  /** Origin the browser opens. */
  webUrl: string;
  webPort: number;
  project: string;
  keep: boolean;
  composeFile: string;
  keeperConfigFile: string;
}

const here = (relative: string) => fileURLToPath(new URL(relative, import.meta.url));

export function e2eEnv(): E2eEnv {
  const external = process.env['LOREBASE_E2E_URL'];
  const keeperPort = num('LOREBASE_E2E_KEEPER_PORT', 18480);
  const webPort = num('LOREBASE_E2E_WEB_PORT', 15174);
  return {
    keeperUrl: (external ?? `http://127.0.0.1:${keeperPort}`).replace(/\/+$/, ''),
    external: external !== undefined && external !== '',
    keeperPort,
    postgresPort: num('LOREBASE_E2E_POSTGRES_PORT', 15532),
    webUrl: `http://127.0.0.1:${webPort}`,
    webPort,
    project: process.env['LOREBASE_E2E_PROJECT'] ?? 'lorebase-e2e',
    keep: process.env['LOREBASE_E2E_KEEP'] === '1',
    composeFile: here('../../../deploy/compose/docker-compose.yaml'),
    keeperConfigFile: here('./keeper.e2e.yaml'),
  };
}

/** Where the global setup hands the Proposals of the run to the workers (JSON). */
export const SCENARIO_ENV = 'LOREBASE_E2E_SCENARIO';

/** The Domain id of this run, set by the global setup so the workers can read it. */
export function runDomain(): string {
  const id = process.env['LOREBASE_E2E_DOMAIN'];
  if (id === undefined) throw new Error('The global setup has not run: no e2e Domain id.');
  return id;
}
