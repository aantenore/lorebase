/**
 * Every user-facing string of the app, by screen, so the copy can be reviewed in one place and
 * translated later. Rules (checked by src/test/domain-agnostic.test.ts):
 *
 * - a heading plus at most one short line; longer explanations live in the docs, linked through
 *   a `docs` path and the "Learn more" link;
 * - no Domain, Kind or Record names from any dataset, and no Domain name interpolated into
 *   descriptions (the Domain is shown by the selector in the top bar);
 * - the product terms are Domain, Kind, Record, Attribute, Link, Relation, Branch, Revision,
 *   Proposal, Source and Document.
 *
 * Strings with values are functions; `docs` entries are paths under the documentation site.
 * Components import the module of their area (`@/messages/review`), not this aggregate, so each
 * lazily loaded screen carries its own strings; the aggregate is for review and tests.
 */
import { admin } from './admin';
import { agentActivity } from './agent-activity';
import { canvas } from './canvas';
import { common } from './common';
import { documents } from './documents';
import { explore } from './explore';
import { explorer } from './explorer';
import { extraction } from './extraction';
import { inference } from './inference';
import { modeler } from './modeler';
import { modelerEditors } from './modeler-editors';
import { reconcile } from './reconcile';
import { record } from './record';
import { retrievalLab } from './retrieval-lab';
import { review } from './review';
import { settings } from './settings';
import { shell } from './shell';
import { sources } from './sources';

export const messages = {
  common,
  shell,
  explorer,
  record,
  canvas,
  modeler,
  modelerEditors,
  inference,
  review,
  reconcile,
  extraction,
  sources,
  documents,
  agentActivity,
  retrievalLab,
  explore,
  settings,
  admin,
} as const;

export type Messages = typeof messages;
