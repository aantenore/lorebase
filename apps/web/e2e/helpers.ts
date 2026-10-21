import type { Page } from '@playwright/test';
import { e2eEnv, runDomain, SCENARIO_ENV } from './env';
import { createKeeper, type Keeper } from './keeper';
import type { ConflictScenario } from './supply';

export const domain = (): string => runDomain();

export const keeper = (): Keeper => createKeeper(e2eEnv().keeperUrl);

/** The Proposals the global setup prepared. */
export function scenario(): ConflictScenario {
  const raw = process.env[SCENARIO_ENV];
  if (raw === undefined) throw new Error('The global setup has not run: no scenario.');
  return JSON.parse(raw) as ConflictScenario;
}

/** The path of a screen of the run's Domain, on a Branch when one is given. */
export function screen(path: string, branch?: string): string {
  const query = branch === undefined ? '' : `?branch=${encodeURIComponent(branch)}`;
  return `/${domain()}/${path}${query}`;
}

/** Fails the test on a failed request or an error in the browser console. */
export function watchForErrors(page: Page): () => string[] {
  const problems: string[] = [];
  page.on('pageerror', (error) => problems.push(`page error: ${error.message}`));
  page.on('response', (response) => {
    // A 304 is a conditional read answered from cache; 4xx and 5xx are Keeper refusing something.
    if (response.status() >= 400 && response.url().includes('/api/v1/')) {
      problems.push(
        `${String(response.status())} ${response.request().method()} ${response.url()}`,
      );
    }
  });
  return () => problems;
}

/** Counts the reads Keeper answered with 304: the fetch layer revalidated them with If-None-Match. */
export function countRevalidated(page: Page): () => number {
  let count = 0;
  page.on('response', (response) => {
    if (response.status() === 304 && response.url().includes('/api/v1/')) count += 1;
  });
  return () => count;
}
