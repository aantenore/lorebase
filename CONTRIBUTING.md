# Contributing to Lorebase

Thank you for your interest in Lorebase. The project is in its design phase; the most valuable contributions right now are design feedback, use cases, and, once M1 starts, implementation of the tasks in [docs/plan/m1.md](docs/plan/m1.md).

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Ways to contribute

- **Design feedback.** Open an issue with the label `design` referencing the section of [docs/design.md](docs/design.md) you are discussing. Decisions marked as architect calls can be challenged with evidence (benchmarks, real use cases, failure scenarios).
- **Use cases.** Describe a domain you would model, the sources you have, and what your agents need to read or change. Real scenarios shape the roadmap.
- **Code.** Pick an M1 task, comment on its tracking issue to claim it, and open a draft pull request early.
- **Documentation.** Fixes and clarifications are always welcome.

## Vocabulary

Lorebase has its own glossary ([docs/glossary.md](docs/glossary.md)). Use it in code, API, UI and docs: Domain, Kind, Attribute, Relation, Trait, Operation, Derivation, Source, Record, Link, Branch, Revision, Proposal, Conflict, Lineage. Pull requests that introduce other names for these concepts will be asked to rename them.

## Development setup (from M1)

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), Docker, Node.js LTS with pnpm (web app, from M2).

```bash
uv sync
make lint type
make test-changed      # tests impacted by your changes only
make test-int          # integration tests (needs Docker for Postgres)
```

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
3. Write commit messages in the imperative mood with a short subject line and a body explaining why when it is not obvious.
4. Sign off your commits (`git commit -s`) to certify the [Developer Certificate of Origin](https://developercertificate.org/).
5. Fill in the pull request template: what changed, why, how it was tested.
6. A maintainer reviews; CI must be green before merge.

## Reporting bugs

Open an issue with steps to reproduce, expected and actual behavior, versions, and relevant logs with secrets removed. For security issues, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

## License

By contributing, you agree that your contributions are licensed under the Apache License 2.0.
