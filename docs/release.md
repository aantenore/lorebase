# Releasing Lorebase

The checklist for a release, what [`release.yaml`](../.github/workflows/release.yaml) does when the tag is pushed, and how to check what it published. A release is a tag `vMAJOR.MINOR.PATCH` (or `-alpha.N`, `-beta.N`, `-rc.N`) on a commit of `main` whose CI is green. The notes are written by hand in [CHANGELOG.md](../CHANGELOG.md) following [the style guide](launch/changelog-style.md).

The workflow never runs on a pull request and nobody creates the tag for you: pushing it is the decision to release.

## Checklist

Work through it on a branch, merge it, then tag. Items marked with a command have a way to prove they are done.

### 1. Version and notes

- [ ] Choose the version. Before 1.0 a minor version may change interfaces; say so in the notes.
- [ ] Set it everywhere it lives: `version` in the root and in each `packages/*/pyproject.toml`, `__version__` in each `packages/*/src/*/__init__.py`, `version` in `apps/web/package.json`, and `appVersion` (and `version`) in `deploy/helm/lorebase/Chart.yaml`.
- [ ] On a new minor version, move the ranges of the dependencies between workspace packages in each `packages/*/pyproject.toml` (`lorebase-core>=0.1,<0.2` becomes `>=0.2,<0.3`; for a pre-release the lower bound may be the pre-release, `>=0.2.0rc1,<0.3`) and run `uv lock`. In a checkout the workspace resolves them from source; on PyPI they keep `pip install lorebase-keeper` from taking a `lorebase-store` of another minor.
- [ ] `uv run python scripts/release_tool.py check --tag vX.Y.Z` prints `all 23 versions and 16 internal ranges agree`. The workflow runs the same check first and stops on a mismatch.
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

- [ ] After the first release, make the three GHCR packages (`lorebase`, `lorebase-ml`, `lorebase-web`) public (Packages, package settings, change visibility) and link them to the repository, so that anyone can pull and the registry page shows the README.
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

It can also be run by hand on a tag that already exists, for example to publish a release whose tag was pushed without the workflow, or to repeat a failed run:

```bash
gh workflow run Release --repo aantenore/lorebase --ref main -f tag=vX.Y.Z
```

The workflow definition comes from `main`; every job checks out the tag, and `verify` stops when the input is not a version tag or the tag does not exist. Signatures of a run started this way carry the identity `release.yaml@refs/heads/main` instead of `@refs/tags/vX.Y.Z` (see [Verify a release](#verify-a-release)).

| Job | What it does |
|-----|--------------|
| `verify` | Checks that every version in the repository equals the tag, that CHANGELOG.md has a section for it, and keeps that section as the release notes. A tag with `-` in it is a pre-release. |
| `sbom` | Calls `sbom.yaml`: CycloneDX documents of the Python workspace (`sbom-python.cdx.json`) and of the web app (`sbom-web.cdx.json`), from the lockfiles. |
| `images` | Builds `deploy/docker/backend.Dockerfile` (twice: the default target and `backend-ml`, the same image plus fastembed and a baked embedding model, used by Scribe and Guide) and `deploy/docker/web.Dockerfile`, pushes `ghcr.io/aantenore/lorebase`, `ghcr.io/aantenore/lorebase-ml` and `ghcr.io/aantenore/lorebase-web` tagged `X.Y.Z`, `X.Y` and `latest` (the last two not for pre-releases), with build provenance. Then signs each image by digest with cosign keyless (the GitHub OIDC identity of the workflow, recorded in the Sigstore transparency log) and attaches the matching SBOM as a CycloneDX attestation. |
| `helm` | Lints the chart, builds its dependency and packages it with `--version` and `--app-version` set to the release version. |
| `python` | `uv build --all-packages`: an sdist and a wheel for each of the ten workspace packages. |
| `release` | Creates the GitHub release with the changelog section as notes and attaches the SBOMs, the chart package, the Python distributions and `SHA256SUMS` with its cosign bundle (`SHA256SUMS.sigstore.json`). When the release exists, it uploads the files to it instead, so a failed run can be repeated. |
| `pypi` | Publishes each package to PyPI from its own environment (`pypi-<short>`), through trusted publishing, only when the repository variable `PYPI_TRUSTED_PUBLISHING` is `true`. Otherwise it prints a notice and does nothing; the packages are on the release. Existing files are skipped, so it is safe to repeat. |

Permissions: the workflow defaults to `contents: read`. Only `images` (`packages: write`, `id-token: write`), `release` (`contents: write`, `id-token: write`) and `pypi` (`id-token: write`) get more. It uses no secret except `GITHUB_TOKEN`.

### PyPI: trusted publishing

The `pypi` job is a matrix over the ten packages and publishes each one with trusted publishing: no token is stored. Package `<short>` (`core`, `platform`, `inference`, `store`, `keeper`, `client`, `cli`, `herald`, `scribe`, `guide`) is the PyPI project `lorebase-<short>`, published from the GitHub environment `pypi-<short>`, with `id-token: write`. The job is enabled by the repository variable `PYPI_TRUSTED_PUBLISHING=true`, and each matrix entry uploads only the wheel and sdist of its own package. `PYPI_API_TOKEN` is no longer used; delete it if it still exists.

Publisher values for each project (PyPI, project Publishing, GitHub tab):

| Field | Value |
|---|---|
| Owner | `aantenore` |
| Repository | `lorebase` |
| Workflow | `release.yaml` |
| Environment | `pypi-<short>`, for example `pypi-core` for `lorebase-core` |

Why one environment per package: PyPI accepts only one pending publisher for the same (owner, repository, workflow, environment) and at most three pending publishers at once, so the ten projects cannot share the environment `pypi`. Create the projects in rounds of three: add a pending publisher for up to three projects, run the release (or repeat it on the same tag: existing files are skipped), and the projects become real. A project that already exists needs a normal trusted publisher with the matching environment `pypi-<short>`. The matrix entries run with `continue-on-error`, so a package whose publisher is not configured yet fails its own job without failing the release. The GitHub environments `pypi-<short>` have no protection rules; the old `pypi` environment is no longer used by the workflow.

For a new project name, add a pending publisher with the same values before the first release.

## Verify a release

Signatures and attestations, with [cosign](https://docs.sigstore.dev/). 0.2.2 was built and published locally, without the release workflow: its images, checksums and assets are not signed and its images carry no SBOM attestation, so these commands apply to every version except 0.2.2. 0.2.3 was published by the release workflow, run by hand on the existing tag, so its signatures carry the `@refs/heads/main` identity; 0.3.0 was published by pushing the tag, so its signatures carry `@refs/tags/v0.3.0`.

```bash
VERSION=0.3.0
# Released by pushing the tag, as 0.3.0 was: the identity ends in @refs/tags/vX.Y.Z.
IDENTITY="^https://github.com/aantenore/lorebase/\.github/workflows/release\.yaml@refs/tags/v${VERSION}\$"
# Run by hand (workflow_dispatch), as 0.2.3 was: the identity ends in @refs/heads/main.
# IDENTITY='^https://github.com/aantenore/lorebase/\.github/workflows/release\.yaml@refs/heads/main$'
ISSUER=https://token.actions.githubusercontent.com

for image in ghcr.io/aantenore/lorebase ghcr.io/aantenore/lorebase-ml ghcr.io/aantenore/lorebase-web; do
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
uv run --with "lorebase-cli==$VERSION" lorebase --version     # from PyPI, once the pypi job has run
```

## Base images

The base images in `deploy/docker/backend.Dockerfile` and `deploy/docker/web.Dockerfile` (and the proxy image in `deploy/compose/docker-compose.yaml` and `.env.example`) are pinned by the digest of their multi-arch index, so `amd64` and `arm64` builds resolve to the same content. The readable tag stays in front of the digest (`python:3.12-slim@sha256:...`). The digests are moved by hand, on purpose: there are no automated version updates.

To bump one:

1. Get the index digest, the top-level `Digest:` line: `docker buildx imagetools inspect python:3.12-slim`. (`docker pull` prints the same digest for a multi-arch tag.)
2. Replace the `@sha256:...` part of the `ARG ..._IMAGE` default (and of the proxy image, if it is the nginx one), keeping the tag.
3. Rebuild both images. The `docker` workflow builds the backend image on pull requests, which verifies the new pin resolves.

`tests/tooling/test_dockerfile_pins.py` fails if a `FROM` of an external image is not pinned by a 64-hex digest.

## Known gaps

State these in the notes of each release until they are closed.

- **No pull request links in the first changelog.** The first section is written from the merged work on `main`, so its lines link the documentation of each feature. From the next release, link the pull request or issue of every line.
