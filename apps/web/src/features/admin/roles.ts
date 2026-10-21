/** Roles a Domain grants (design 10.2). Keeper accepts custom roles too; these are the built-in ones. */
export const ROLES = [
  'agent-reader',
  'viewer',
  'explorer',
  'editor',
  'operator',
  'modeler',
  'reviewer',
  'maintainer',
  'admin',
] as const;

/** The role Keeper gives a new service principal when the request names none. */
export const DEFAULT_KEY_ROLE: (typeof ROLES)[number] = 'agent-reader';
