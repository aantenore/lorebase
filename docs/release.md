# Releasing Lorebase

The checklist for a release, what [`release.yaml`](../.github/workflows/release.yaml) does when the tag is pushed, and how to check what it published. A release is a tag `vMAJOR.MINOR.PATCH` (or `-alpha.N`, `-beta.N`, `-rc.N`) on a commit of `main` whose CI is green. The notes are written by hand in [CHANGELOG.md](../CHANGELOG.md) following [the style guide](launch/changelog-style.md).

The workflow never runs on a pull request and nobody creates the tag for you: pushing it is the decision to release.

## Checklist

Work through it on a branch, merge it, then tag. Items marked with a command have a way to prove they are done.

### 1. Version and notes

- [ ] Choose the version. Before 1.0 a minor version may change interfaces; say so in the notes.
- [ ] Set it everywhere it lives: `version` in the root and in each `packages/*/pyproject.toml`, `__version__` in each `packages/*/src/*/__init__.py`, `version` in `apps/web/package.json`, and `appVersion` (and `version`) in `deploy/helm/lorebase/Chart.yaml`.
- [ ] On a new minor version, move the ranges of the dependencies between workspace packages in each `packages/*/pyproject.toml` (`lorebase-core>=0.1,<0.2` becomes `>=0.2,<0.3`; for a pre-release the lower bound may be the pre-release, `>=0.2.0rc1,<0.3`) and run `uv lock`. In a checkout the workspace resolves them from source; on PyPI they keep `pip install lorebase-keeper` from taking a `lorebase-store` of another minor.
- [ ] `uv run python scripts/release_tool.py check --tag vX.Y.Z` prints `all 21 versions and 12 internal ranges agree`. The workflow runs the same check first and stops on a mismatch.
- [ ] Write the `## X.Y.Z` section of CHANGELOG.md: summary, breaking changes first, then added, changed, fixed, security, not done yet, upgrade notes. Link the pull request or issue of each line. `uv run python scripts/release_tool.py notes X.Y.Z` prints what the GitHub release will say.
- [ ] A migration, a configuration key or an API response that changed is named in the first lines of the section.
- [ ] Update the status wording that a release makes false: the "Pre-alpha" banner and "Nothing is released" in [README.md](../README.md) and [roadmap.md](roadmap.md).

### 2. Tests

- [ ] `make lint type test-unit`
- [ ] `make test-int` (needs Docker)
- [ ] `make openapi` leaves `docs/openapi.json` unchanged
- [ ] Web app, in `apps/web`: `pnpm lint`, `pnpm typecheck`, `pnpm test`, `pnpm build`
- [ ] `make demo`: the five-day demo runs to the end against a fresh stack ([demo](demo.md))
- [ ] CI is green on the commit to be tagged, including the `helm` job (`helm lint --strict`, `helm template` for every profile, kubeconform)

The full suite is for releases; day to day, `make test-changed` is enough.

### 3. Performance baseline

- [ ] `make test-perf` at the scale documented in [performance.md](performance.md); `tests/perf/bench.py` writes the baseline.
- [ ] Compare it with the figures in README.md and performance.md. Update them only with numbers the harness reproduces, and name the hardware. A figure the harness cannot reproduce does not go into the notes.

### 4. Security scans

- [ ] `make security`: `pip-audit` on the locked runtime dependencies and `pnpm audit --audit-level=high`, the same checks as the Audit workflow. Advisories with a fixed version are fixed; the ones in `.github/audit-allowlist.yaml` are reviewed, renewed or removed, and listed in the notes.
- [ ] CodeQL and Scorecard have run on `main` and their open alerts are triaged (the Security tab).
- [ ] [SECURITY.md](../SECURITY.md) still names the right contact and supported versions.
- [ ] The latest Audit workflow run on `main` is green, and no allowlist entry in `.github/audit-allowlist.yaml` has expired.
- [ ] SBOMs: the workflow generates them (`sbom.yaml`); `workflow_dispatch` on that workflow shows them before the tag if you want to look first.

### 5. Documentation

- [ ] [quickstart.md](quickstart.md), [deployment.md](deployment.md), [kubernetes.md](kubernetes.md), [mcp.md](mcp.md) and [operations.md](operations.md) describe what the release does; commands in them still work.
- [ ] Every "coming next" or "not built yet" sentence that the release makes false is gone, and CHANGELOG "Not done yet" matches [roadmap.md](roadmap.md).
- [ ] [demo.md](demo.md) matches what `make demo` prints.
- [ ] The glossary vocabulary is used in the notes: Domain, Kind, Record, Revision, Proposal, Conflict, Operation.

### 6. Tag

- [ ] From an up-to-date `main`: `git tag -s vX.Y.Z -m "Lorebase X.Y.Z"` and `git push origin vX.Y.Z`. (Use `-a` when you cannot sign.)
- [ ] Watch the workflow ([What the workflow does](#what-the-workflow-does)).

### 7. Images

- [ ] After the first release, make the two GHCR packages public (Packages, package settings, change visibility) and link them to the repository, so that anyone can pull and the registry page shows the README.
- [ ] Verify the signatures ([Verify a release](#verify-a-release)).

### 8. Helm

- [ ] `helm template` of the chart package from the release assets renders with `--set web.enabled=true`.
- [ ] The chart's default image tag is its `appVersion`, so `image.tag` can stay empty; check that the images of that version exist.
- [ ] Optional: publish the chart to an OCI registry (`helm push lorebase-X.Y.Z.tgz oci://ghcr.io/aantenore/charts`). The workflow only attaches the package to the release.

### 9. Announce

- [ ] The GitHub release text is the changelog section; read it once in the browser.
- [ ] The launch kit in [docs/launch](launch) has the posts: [blog post](launch/blog-post.md), [Hacker News](launch/hacker-news.md), [Reddit](launch/reddit.md), [LinkedIn](launch/linkedin.md), [awesome lists](launch/awesome-lists.md). Post the release link and the demo command (`make demo`), not a feature list.
- [ ] Answer questions in the first days from the FAQ ([faq.md](faq.md)); anything asked twice becomes a line in the FAQ.

## What the workflow does

`release.yaml` runs on a pushed tag `v*`. Every action is pinned to a commit SHA.

| Job | What it does |
|-----|--------------|
| `verify` | Checks that every version in the repository equals the tag, that CHANGELOG.md has a section for it, and keeps that section as the release notes. A tag with `-` in it is a pre-release. |
| `sbom` | Calls `sbom.yaml`: CycloneDX documents of the Python workspace (`sbom-python.cdx.json`) and of the web app (`sbom-web.cdx.json`), from the lockfiles. |
| `images` | Builds `deploy/docker/backend.Dockerfile` and `deploy/docker/web.Dockerfile`, pushes `ghcr.io/aantenore/lorebase` and `ghcr.io/aantenore/lorebase-web` tagged `X.Y.Z`, `X.Y` and `latest` (the last two not for pre-releases), with build provenance. Then signs each image by digest with cosign keyless (the GitHub OIDC identity of the workflow, recorded in the Sigstore transparency log) and attaches the matching SBOM as a CycloneDX attestation. |
| `helm` | Lints the chart, builds its dependency and packages it with `--version` and `--app-version` set to the release version. |
| `python` | `uv build --all-packages`: an sdist and a wheel for each of the nine workspace packages. |
| `release` | Creates the GitHub release with the changelog section as notes and attaches the SBOMs, the chart package, the Python distributions and `SHA256SUMS` with its cosign bundle (`SHA256SUMS.sigstore.json`). When the release exists, it uploads the files to it instead, so a failed run can be repeated. |
| `pypi` | Publishes the distributions to PyPI, only when the repository secret `PYPI_API_TOKEN` exists. Otherwise it prints a notice and does nothing; the packages are on the release. Existing files are skipped, so it is safe to repeat. |

Permissions: the workflow defaults to `contents: read`. Only `images` (`packages: write`, `id-token: write`), `release` (`contents: write`, `id-token: write`) and `pypi` (`id-token: write`) get more. It uses no secret except `GITHUB_TOKEN` and the optional `PYPI_API_TOKEN`.

### PyPI: trusted publishing is preferred

A long-lived `PYPI_API_TOKEN` works and is what the workflow checks for. Trusted publishing needs no stored secret and is the better choice once the nine projects exist on PyPI:

1. On PyPI, for each project (`lorebase-core`, `lorebase-platform`, `lorebase-inference`, `lorebase-store`, `lorebase-keeper`, `lorebase-client`, `lorebase-cli`, `lorebase-herald`, `lorebase-scribe`), add a trusted publisher: owner `aantenore`, repository `lorebase`, workflow `release.yaml`, environment `pypi`. For projects that do not exist yet, add a pending publisher with the same values.
2. In `release.yaml`, remove the `password:` line of the publish step and change the guard of the `pypi` job from the secret to a repository variable (for example `vars.PYPI_TRUSTED_PUBLISHING == 'true'`). The job already has `id-token: write` and `environment: pypi`.
3. Delete the `PYPI_API_TOKEN` secret.

The first upload of a name cannot use a project-scoped token (the project does not exist), so a first release by token needs an account-scoped token that is replaced by trusted publishing right after.

## Verify a release

Signatures and attestations, with [cosign](https://docs.sigstore.dev/):

```bash
VERSION=0.1.0
IDENTITY='^https://github.com/aantenore/lorebase/\.github/workflows/release\.yaml@refs/tags/v'
ISSUER=https://token.actions.githubusercontent.com

for image in ghcr.io/aantenore/lorebase ghcr.io/aantenore/lorebase-web; do
  cosign verify "$image:$VERSION" \
    --certificate-identity-regexp "$IDENTITY" --certificate-oidc-issuer "$ISSUER"
  cosign verify-attestation --type cyclonedx "$image:$VERSION" \
    --certificate-identity-regexp "$IDENTITY" --certificate-oidc-issuer "$ISSUER"
done
```

The release assets, against their signed checksums:

```bash
gh release download "v$VERSION" --repo aantenore/lorebase --dir release
cd release
cosign verify-blob SHA256SUMS --bundle SHA256SUMS.sigstore.json \
  --certificate-identity-regexp "$IDENTITY" --certificate-oidc-issuer "$ISSUER"
sha256sum --check SHA256SUMS
```

A smoke test of what was published:

```bash
docker run --rm "ghcr.io/aantenore/lorebase:$VERSION" python -c "import lorebase_keeper, lorebase_store"
helm template check "release/lorebase-$VERSION.tgz" -n lorebase >/dev/null
uv run --with "lorebase-cli==$VERSION" lorebase --version     # only when published to PyPI
```

## Known gaps

State these in the notes of each release until they are closed.

- **No pull request links in the first changelog.** The first section is written from the merged work on `main`, so its lines link the documentation of each feature. From the next release, link the pull request or issue of every line.
