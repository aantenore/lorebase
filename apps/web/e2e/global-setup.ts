import { e2eEnv, SCENARIO_ENV } from './env';
import { createKeeper, waitForKeeper } from './keeper';
import { startStack, stopStack } from './stack';
import { dropDomain, loadSupply, prepareConflicts } from './supply';

/**
 * Brings up the stack (unless LOREBASE_E2E_URL names a Keeper), loads the supply Domain under an
 * id made for this run, and prepares the Conflict scenario. The function it returns is the
 * teardown: it drops the Domain and stops the stack it started.
 */
export default async function globalSetup(): Promise<() => Promise<void>> {
  const env = e2eEnv();
  if (env.external) await waitForKeeper(env.keeperUrl, 60_000);
  else await startStack(env);

  const keeper = createKeeper(env.keeperUrl);
  const domain = `e2e-${Date.now().toString(36)}`;
  await loadSupply(keeper, domain);
  const scenario = await prepareConflicts(keeper, domain);
  process.env['LOREBASE_E2E_DOMAIN'] = domain;
  process.env[SCENARIO_ENV] = JSON.stringify(scenario);

  return async () => {
    if (env.external) {
      await dropDomain(keeper, domain).catch(() => undefined);
    } else if (!env.keep) {
      stopStack(env);
    }
  };
}
