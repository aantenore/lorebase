# Contributing to Lorebase

Thank you for your interest in Lorebase. The project is pre-alpha and M1 is in progress; the most valuable contributions right now are design feedback, real use cases, and implementation of the open tasks in [docs/plan/m1.md](docs/plan/m1.md) (see the [roadmap](docs/roadmap.md) for what is done).

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Ways to contribute

- **Design feedback.** Open an issue with the label `design` referencing the section of [docs/design.md](docs/design.md) you are discussing. Decisions marked as architect calls can be challenged with evidence (benchmarks, real use cases, failure scenarios).
- **Use cases.** Describe a domain you would model, the sources you have, and what your agents need to read or change. Real scenarios shape the roadmap.
- **Code.** Pick an M1 task, comment on its tracking issue to claim it, and open a draft pull request early.
- **Documentation.** Fixes and clarifications are always welcome.

## Vocabulary

Lorebase has its own glossary ([docs/glossary.md](docs/glossary.md)). Use it in code, API, UI and docs: Domain, Kind, Attribute, Relation, Trait, Operation, Derivation, Source, Record, Link, Branch, Revision, Proposal, Conflict, Lineage. Pull requests that introduce other names for these concepts will be asked to rename them.

## Development workflow

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), Docker (integration tests), Node.js 24 with pnpm (web app in `apps/web`).

```bash
uv sync --all-packages            # install every workspace package and the dev tools
make lint type test-unit          # ruff and import contracts, mypy, unit tests
make test-changed                 # only the tests impacted by your diff
make test-int                     # integration tests (needs Docker for Postgres)
make security                     # dependency advisories (see below)
```

1. Branch from `main` (`feat/...`, `fix/...`, `docs/...`).
2. Make a focused change with tests, then run `make lint type test-changed` before pushing. The full suite runs in CI on every pull request.
3. Write the commit subject in conventional form: imperative mood, at most 72 characters, no trailing period, optional type prefix (`feat:`, `fix:`, `docs:`, `chore:`, `ci:`). Explain why in the body when it is not obvious.
4. Commits carry the author only: no trailers (`Signed-off-by`, `Co-authored-by` and similar).
5. Open a pull request; CI must be green before merge.

### Dependency advisories

`make security` exports the locked Python runtime dependencies with `uv export` and checks them with `uvx pip-audit`, then runs `pnpm audit --audit-level=high` in `apps/web`. Advisories that have no fixed version yet are reported but do not fail the target; any advisory with a fix does. It needs network access. Dependabot opens weekly grouped update pull requests for Python, npm, GitHub Actions and container images.

### Testing policy

- Locally, run the tests that cover what you changed (`make test-changed`) and the fast validators. The full suite runs in CI on every pull request.
- New behavior needs tests. The core versioning engine needs property-based tests for any change to hashing, trees, diff or merge.
- Integration tests run `admin verify` at teardown; keep it passing.

### Code guidelines

- Respect package boundaries (design section 17); import-linter enforces them.
- No hardcoded configuration: endpoints, limits, model names and credentials come from settings.
- Add dependencies only with a stated reason in the pull request.
- Type hints everywhere; `mypy --strict` for `core`, `store` and `keeper`.
- Keep public APIs documented with short docstrings; update the design doc when behavior changes.

## Pull requests

1. Fork and branch from `main` (`feat/...`, `fix/...`, `docs/...`).
2. Keep pull requests focused on one task or fix.
3. Follow the commit conventions in [Development workflow](#development-workflow).
4. Fill in the pull request template: what changed, why, how it was tested.
5. A maintainer reviews; CI must be green before merge.

## Labels

Issues and pull requests use these labels.

| Label | Meaning |
|-------|---------|
| `bug`, `enhancement`, `question` | Set by the issue templates |
| `design` | Feedback on [docs/design.md](docs/design.md) |
| `good first issue` | Small, well-scoped task suited to a first contribution |
| `help wanted` | Maintainers would welcome a contribution |
| `dependencies` | Dependency update pull requests (Dependabot) |
| `area/core` | Engine: hashing, trees, diff, merge, schema |
| `area/store` | Postgres schema and migrations |
| `area/keeper` | REST API, auth, commit pipeline |
| `area/inference` | Model abstraction and extraction |
| `area/client` | Python client and CLI |
| `area/backend` | Python workspace as a whole, tooling and dependencies |
| `area/web` | Web app in `apps/web` |
| `area/deploy` | Docker images and compose |
| `area/ci` | Workflows and repository automation |
| `area/docs` | Documentation |

## Reporting bugs

Open an issue with steps to reproduce, expected and actual behavior, versions, and relevant logs with secrets removed. For security issues, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

## License

By contributing, you agree that your contributions are licensed under the Apache License 2.0.
