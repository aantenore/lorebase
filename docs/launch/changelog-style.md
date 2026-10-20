# How we write release notes

Release notes are written for the person deciding whether to upgrade. They say what changed in terms of what that person can do, what might break, and what is still missing. The rules below apply to the changelog, to GitHub release descriptions and to announcement posts.

## Versioning

Lorebase follows semantic versioning. Before 1.0, minor versions (0.x) may change interfaces, and the notes say so. From 1.0, `/api/v1` is stable and breaking changes need a new major version. A release that changes a database migration, a configuration key or an API response says so in the first lines, not in a footnote.

## Structure

Each release has the same sections, in this order. Omit a section that is empty.

1. **Summary.** One to three sentences: the most important change and the state of the project. No marketing.
2. **Breaking changes.** What breaks, who is affected, and what to do. Include the migration steps or the configuration key to change.
3. **Added.** New capabilities, as things a user can do.
4. **Changed.** Behavior that differs from the previous release without breaking it.
5. **Fixed.** Bugs, with the symptom a user would have seen, not the code that was wrong.
6. **Security.** Vulnerabilities fixed, with the advisory link. Follow [SECURITY.md](../../SECURITY.md) for disclosure timing.
7. **Not done yet.** The things a reader of the previous release might expect and that are still missing. This section keeps the project honest.
8. **Upgrade notes.** Order of operations: migrations, configuration, restarts.

## Writing rules

- Lead with the user-visible effect. "Branches can now be merged through `POST /domains/{id}/proposals/{n}/merge`", not "Added merge service".
- Name the endpoint, the command, the setting or the file. A reader should be able to search for it.
- Use the project vocabulary from the [glossary](../glossary.md): Domain, Kind, Record, Revision, Proposal, Conflict, Operation.
- State limits. If a feature works only for one Kind of input, or only on the primary, say so in the same line.
- One change per line, in the present or past tense, no trailing period required but consistent within a release.
- Link the pull request or issue for each line. Do not list commits.
- Do not use hype words, superlatives, emoji or exclamation marks. "Faster" needs a number and a method, or it is not written.
- Do not publish a performance figure that is not reproducible with the harness in the repository.
- Credit contributors by handle at the end of the line when the change came from outside the maintainers.

## What does not go in

- Refactors, dependency bumps and CI changes with no user-visible effect, unless they affect supported versions (Python, Node, Postgres) or the image.
- Internal task identifiers.
- Promises about future releases, except in the "Not done yet" section, and only as "not done", never as a date.

## Commit messages

Release notes are written by hand from the merged pull requests, not generated from commit subjects. Commit subjects follow [CONTRIBUTING.md](../../CONTRIBUTING.md): imperative mood, at most 72 characters, no trailers.

## Example

The example is illustrative: the endpoints, numbers and issue ids are placeholders for the format, not a statement about what exists.

```markdown
## 0.2.0

Proposals, merge and Conflict resolution are now available through the REST API, so a protected
branch can be changed end to end without touching the database. Pre-1.0: interfaces may still
change.

### Breaking changes

- `POST /domains/{id}/operations/{name}/execute` on a protected branch now answers `409` with a
  link to `POST /domains/{id}/proposals` instead of the earlier `protected-branch` problem type.
  Clients that matched the old type must match `proposal-required`. (#128)

### Added

- `POST /domains/{id}/proposals` opens a Proposal from a source to a target branch and returns
  the merge preview with the Conflict count. (#121)
- `GET /domains/{id}/proposals/{n}/conflicts` lists Conflicts grouped by code, and
  `PUT .../conflicts/{fingerprint}` resolves one with ours, theirs, base or a custom value. (#124)
- `POST .../merge` merges when no Conflict is open, or answers `409` with the open ones. (#126)

### Fixed

- A diff between two Revisions of a Domain with more than one Relation no longer lists the same
  Link twice. (#119)

### Not done yet

- Review policies and required approvals. Proposals merge without approvals in this release.
- The CLI and the Python client.
- The MCP server.

### Upgrade notes

Run the migration job before starting the new Keeper: it adds the Proposal tables.
```
