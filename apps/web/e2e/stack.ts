/**
 * The compose stack of the run: Postgres, the migrations and Keeper, from
 * deploy/compose/docker-compose.yaml under a project name of its own (`lorebase-e2e`), its own
 * image name and its own host ports, so it never touches a stack the developer already has.
 */
import { spawnSync } from 'node:child_process';
import type { E2eEnv } from './env';
import { waitForKeeper } from './keeper';

const READY_TIMEOUT_MS = 180_000;

function composeEnv(env: E2eEnv): NodeJS.ProcessEnv {
  return {
    ...process.env,
    // Throwaway database of the run; the port is bound to loopback by the compose file.
    POSTGRES_PASSWORD: process.env['LOREBASE_E2E_POSTGRES_PASSWORD'] ?? 'lorebase-e2e',
    POSTGRES_HOST_PORT: String(env.postgresPort),
    KEEPER_HOST_PORT: String(env.keeperPort),
    LOREBASE_IMAGE: process.env['LOREBASE_E2E_IMAGE'] ?? 'lorebase-e2e',
    LOREBASE_TAG: 'run',
    LOREBASE_CONFIG_FILE: env.keeperConfigFile,
    // auth.mode none on 0.0.0.0 inside the container, published on loopback only: both set here
    // so a developer's own values never apply to the run.
    KEEPER_BIND: '127.0.0.1',
    KEEPER_ALLOW_INSECURE: 'true',
  };
}

function compose(env: E2eEnv, args: string[], allowFailure = false) {
  const result = spawnSync(
    'docker',
    ['compose', '-p', env.project, '-f', env.composeFile, ...args],
    { env: composeEnv(env), stdio: 'inherit' },
  );
  if (result.error !== undefined) {
    throw new Error(
      `Could not run docker (${result.error.message}). Install Docker, or point LOREBASE_E2E_URL at a running Keeper.`,
    );
  }
  if (result.status !== 0 && !allowFailure) {
    throw new Error(`docker compose ${args.join(' ')} exited with ${String(result.status)}.`);
  }
}

/** Builds and starts the stack, and waits until Keeper reports ready. */
export async function startStack(env: E2eEnv): Promise<void> {
  compose(env, ['up', '-d', '--build', 'postgres', 'migrate', 'keeper']);
  try {
    await waitForKeeper(env.keeperUrl, READY_TIMEOUT_MS);
  } catch (error) {
    compose(env, ['logs', '--tail', '80', 'keeper', 'migrate'], true);
    throw error;
  }
}

/** Removes the containers, network and database volume of the run. */
export function stopStack(env: E2eEnv): void {
  compose(env, ['down', '--volumes', '--remove-orphans'], true);
}
