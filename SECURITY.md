# Security Policy

## Supported versions

Lorebase is pre-release. Until 1.0, security fixes are applied to the latest release and to `main` only.

| Version | Supported |
|---------|-----------|
| `main` | yes |
| latest `0.x` release | yes |
| older `0.x` releases | no |

## Reporting a vulnerability

Please do not open public issues for security problems.

Report vulnerabilities privately through GitHub's private vulnerability reporting on this repository (Security tab, "Report a vulnerability"). Include:

- a description of the issue and its impact;
- steps to reproduce or a proof of concept;
- affected versions or commits;
- any suggested mitigation.

You will receive an acknowledgement within 5 working days and a first assessment within 10 working days. We will keep you informed of progress, agree on a disclosure date with you, and credit you in the advisory unless you prefer otherwise.

## Scope

In scope: the Keeper API, Scribe workers, Herald MCP server, CLI, web app, container images, Helm chart and default configuration published from this repository.

Particularly relevant classes of issues:

- authentication or authorization bypass (Domain isolation, RBAC scopes, sensitivity masking);
- writes that bypass Operations, review policies or branch protection;
- tampering with Revisions, history or the audit log;
- injection in the query compiler or expression evaluation;
- server-side request forgery through Sources or webhook targets;
- leakage of secrets or sensitive Attribute values through logs, traces, search or exports;
- extraction pipeline issues that let document content cause actions beyond proposing values.

Out of scope: vulnerabilities in third-party dependencies without a demonstrated impact on Lorebase (report them upstream), denial of service through deliberately unlimited configuration, and issues requiring an already compromised host or database.

## Hardening guidance

Deployment hardening guidance is part of the design ([docs/design.md](docs/design.md), section 13) and will be expanded into an operations guide before 1.0.
