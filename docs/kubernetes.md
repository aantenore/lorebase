# Kubernetes

Lorebase on Kubernetes with the Helm chart in `deploy/helm/lorebase`. Design references:
[15.3](design.md#153-production) (production), [16](design.md#16-configuration) (configuration).
Topology, read replicas and sizing are in [deployment.md](deployment.md); this page is the
cluster side of it.

The chart is the counterpart of `deploy/compose/docker-compose.yaml`: the same single backend
image (the command selects the service), the same split between deployment values and the
application's own `lorebase.yaml`, and the same environment variables.

## What the chart installs

| Part | Default | Notes |
|------|---------|-------|
| Keeper (Deployment, Service, PDB, optional HPA) | on | Probes `/healthz` and `/readyz`, non-root, read-only root file system, topology spread |
| Migration (Helm hook Job) | on | `alembic upgrade head` before Keeper rolls out |
| ConfigMap | on | The `config` values block, mounted as `/etc/lorebase/lorebase.yaml` |
| Secret | on, unless `secrets.existingSecret` | DSNs, API key pepper, LLM key; never in the ConfigMap |
| ServiceAccount | on | No API token mounted |
| Scribe (Deployment, optional HPA, PDB) | off | `lorebase-scribe`, the job worker and scheduler ([scribe.md](scribe.md)) |
| Herald, MCP over streamable HTTP (Deployment, Service, PDB, optional HPA) | off | |
| Web app (nginx Deployment, Service, runtime config) | off | |
| Ollama (Deployment, Service, PVC) | off | Local models profile; pulls the model on start |
| Ingress, NetworkPolicy, ServiceMonitor | off | |
| CloudNativePG Cluster | off | Optional in-cluster Postgres, subchart `postgresql` |

Postgres 16 with pgvector is required. Production uses an external Postgres (managed or
operator-run) passed as a DSN.

## Requirements

- Kubernetes 1.27 or newer and Helm 3.
- Postgres 16 with the `vector` extension, reachable from the cluster.
- An ingress controller for external access; metrics-server for the CPU based HPA.
- Optional: the CloudNativePG operator (in-cluster Postgres), the Prometheus Operator CRDs
  (ServiceMonitor), a CNI that enforces NetworkPolicy.

## Install

The chart is installed from the repository; it is not published to a chart repository yet.

```bash
# 1. The image. Use the published one, or build and push your own:
docker build -f deploy/docker/backend.Dockerfile -t registry.example.com/lorebase:0.1.0 .
docker push registry.example.com/lorebase:0.1.0

# 2. The chart declares one optional dependency (CloudNativePG); fetch it once, even if unused.
helm dependency build deploy/helm/lorebase

# 3. Secrets. Keep them out of values files that are committed.
kubectl create namespace lorebase
kubectl -n lorebase create secret generic lorebase-secrets \
  --from-literal=db-dsn='postgresql+asyncpg://lorebase:<password>@postgres.example.internal:5432/lorebase' \
  --from-literal=api-key-pepper="$(openssl rand -hex 32)"

# 4. Install with a profile (below) and your overrides.
helm upgrade --install lorebase deploy/helm/lorebase -n lorebase \
  --set secrets.existingSecret=lorebase-secrets \
  --set image.repository=registry.example.com/lorebase --set image.tag=0.1.0

helm -n lorebase test lorebase          # calls /readyz through the Keeper Service
```

Use a DSN with URL-safe characters in the password. `postgresql://` URLs are switched to the
asyncpg driver by the application, so either form works.

Without `existingSecret`, the chart creates the Secret from `secrets.values.*`. Pass those with
`--set-file` or a values file that stays out of git; the chart renders no Secret entry for a value left empty.

## Configuration model

Three layers, like compose:

1. `config` (values) is the application's `lorebase.yaml`, rendered into a ConfigMap. Same keys and
   defaults as `config/lorebase.example.yaml`. Everything is optional; the chart sets
   `keeper.auth.mode: api_key`, because `none` accepts every request as an administrator.
2. Environment variables set by the chart override the file, for the deployment-level values only:
   `LOREBASE__KEEPER__HOST/PORT`, `keeper.preferReplica`, `logLevel`, `telemetry.*`, the DSNs, the
   pepper, the Ollama and LLM settings of Scribe. Do not repeat those in `config`.
3. Secrets are referenced as `secret://env/NAME`; the chart maps Secret keys to those variables.
   `secrets.extra` (chart-managed) or `secrets.extraNames` (existing Secret) add your own, for
   example hosted embedding credentials.

The chart refuses to render when `keeper.auth.mode` needs an API key pepper and none is given, when no
database is configured, when `ollama` is on without `scribe`, and when the in-cluster Postgres has no
pgvector image. Pods roll when the ConfigMap or the chart-managed Secret changes (checksum annotations).

## Profiles

The three profiles are values files in `deploy/helm/lorebase/ci/`; CI lints and renders them.
Layer them with repeated `-f`.

### Minimal

One Keeper, an external Postgres, API keys. For a trial. `ci/minimal-values.yaml`:

```yaml
keeper:
  replicaCount: 1
  pdb:
    enabled: false
secrets:
  values:                      # replace; or use secrets.existingSecret as above
    dbDsn: postgresql+asyncpg://lorebase:<password>@postgres.example.internal:5432/lorebase
    apiKeyPepper: <32 characters or more>
```

```bash
helm upgrade --install lorebase deploy/helm/lorebase -n lorebase -f deploy/helm/lorebase/ci/minimal-values.yaml
kubectl -n lorebase port-forward svc/lorebase-keeper 8080:8080
```

### Production

`ci/production-values.yaml`: a Secret you manage, a pinned image tag, OIDC next to API keys, 3 Keepers
with larger requests, Herald and the web app, an Ingress with TLS, NetworkPolicy with egress
restricted to Postgres, the collector and HTTPS, OTLP export, and a ServiceMonitor on the collector.
Edit the hosts, CIDRs and issuer. Key points:

```yaml
image: {tag: "0.1.0"}
secrets: {existingSecret: lorebase-secrets}
config:
  keeper: {auth: {mode: api_key+oidc, oidc: {issuer: https://login.example.com/realms/lorebase, audience: [lorebase]}}}
keeper: {replicaCount: 3}
ingress: {enabled: true, className: nginx, tls: [{secretName: lorebase-tls, hosts: [lorebase.example.com]}]}
networkPolicy: {enabled: true}
telemetry: {otlpEndpoint: "http://otel-collector.observability.svc:4317"}
```

The Keeper path of the Ingress (`/api`) must match `keeper.api_prefix` (`/api/v1`) without the
version. Set the controller's body limit above `keeper.limits.max_request_body_bytes` (1 MiB).

### Scale-out

`ci/scale-out-values.yaml`, layered on production (design: [deployment.md](deployment.md)):

```bash
helm upgrade --install lorebase deploy/helm/lorebase -n lorebase \
  -f deploy/helm/lorebase/ci/production-values.yaml \
  -f deploy/helm/lorebase/ci/scale-out-values.yaml
```

- **Read replicas.** Add the replica DSNs to the Secret as a comma separated list under the
  `db-read-dsn` key (`secrets.values.dbReadDsn` accepts a list) and set `keeper.preferReplica: true`.
  Reads then go to a replica that passes the freshness guard; writes and pinned reads stay on the
  primary. `/readyz` reports `database_replicas`.
- **Keepers.** The HPA runs 4 to 20 Keepers on CPU (65 percent of requests) and on a custom metric
  placeholder (`lorebase_http_requests_per_second`, 50 per pod). The services export OTLP only, so that
  metric needs an adapter you provide (Prometheus Adapter or KEDA fed from the collector); remove the
  entry to scale on CPU alone. `behavior` slows scale-down to 5 minutes.
- **Connections.** Each Keeper may open `pool_size + max_overflow` connections (20 by default) per
  database. Size `max_replicas x 20` below Postgres `max_connections`, or lower `database.pool_size`
  and `max_overflow` in `config`. A pooler in front is untested (see [deployment.md](deployment.md)).
- **Scribe.** Workers pull jobs with `SKIP LOCKED` and the leader is elected inside the application,
  so replicas can be changed freely; the HPA scales on queue depth through an external metric you provide.
- **Spread.** Keeper pods are spread over zones and nodes (soft constraints) and a PDB allows one
  unavailable pod at a time.

### Local models

`ci/local-models-values.yaml` adds Ollama and the in-cluster Postgres to a trial. Ollama runs as a single
pod with a PVC; a sidecar pulls `ollama.model` and the pod turns Ready when the model is listed.
Scribe receives `LLM_BASE_URL` and `LLM_MODEL` for it. GPU nodes: add `nvidia.com/gpu: 1` to
`ollama.resources.limits` and a node selector or toleration.

The in-cluster Postgres is the CloudNativePG `cluster` chart under the alias `postgresql`. It needs
the CloudNativePG operator and an image with pgvector (`postgresql.cluster.imageName`, required: the
default CloudNativePG image does not include it). The chart reads the connection string from the
operator's `<release>-postgresql-app` Secret. Set `postgresql.cluster.instances` above 1 for standbys;
point `secrets.values.dbReadDsn` at the `-ro` Service for reads. Not recommended for production data
unless you operate CloudNativePG, backups included.

## Upgrade and migrations

`helm upgrade` runs a `pre-upgrade` Job (`alembic upgrade head`) before any Deployment changes.
The Job gets its own ConfigMap (`database`, `store` and `logging` of `config`, because the initial
migration reads `store.layout` and `store.embeddings`) and, for a chart-managed DSN, its own Secret,
created as hooks so that they exist before a first install.

- **Order.** Migration first, then a rolling update: `maxSurge: 1`, `maxUnavailable: 0`, readiness on
  `/readyz` (which also checks that migrations are current), `preStop` sleep and a termination grace
  period so in-flight requests finish.
- **Failure.** If the Job fails (after `migrate.backoffLimit` retries or `migrate.activeDeadlineSeconds`)
  the upgrade stops and the running pods keep serving the old version. The failed Job and its logs
  stay for inspection (`kubectl -n lorebase logs job/lorebase-migrate`); the next attempt replaces it.
- **Rollback.** Migrations are forward-only and online-safe (expand, migrate, contract over releases), so
  the previous release runs against the new schema. `helm rollback` does not reverse a migration.
- **First install** with an external database uses `pre-install`. With `postgresql.enabled` the database
  does not exist yet, so the hook runs as `post-install` and waits for the database (`migrate.waitForDatabase`);
  do not combine that first install with `--wait`, because Keeper is not ready until the migration ran.
- **Manual control.** `migrate.enabled=false` skips the Job; run `alembic upgrade head` from
  `/opt/lorebase/store` with the same image and settings yourself (for example from a pipeline).
- The hook Job uses the namespace `default` ServiceAccount (`migrate.serviceAccountName` overrides it),
  because the chart's own ServiceAccount does not exist before the first install.

## Exposure, network and security

- Pods run as uid 10001, drop all capabilities, forbid privilege escalation, use the runtime default
  seccomp profile, and have a read-only root file system (`/tmp` is an `emptyDir`). Override through
  `podSecurityContext` and `containerSecurityContext`.
- `networkPolicy.enabled` closes ingress for every pod of the release and opens Keeper to the other
  release pods and to `networkPolicy.ingressFrom` (default: the `ingress-nginx` namespace), and Herald and
  the web app to the same peers. `egress.enabled` also restricts egress to DNS, the release and `egress.rules`.
- Herald forwards each caller's own API key to Keeper and holds no credential. Its readiness is a TCP
  check because it serves only the MCP endpoint. Set `herald.http.allowed_hosts` and `allowed_origins` in
  `config` for DNS rebinding protection behind your hostname.
- API key mode needs the pepper (32 characters or more); changing it invalidates every issued key.

## Web app

`web.enabled` runs an unprivileged nginx with the production build (image `ghcr.io/aantenore/lorebase-web`,
design 15.1). The chart serves `/config.js` from a ConfigMap, setting `window.__LOREBASE_CONFIG__` with the
names of the Vite variables (`VITE_API_MODE`, `VITE_KEEPER_BASE_URL`, plus `web.config.extra`), so one image works
against any Keeper; `keeperBaseUrl` defaults to the first Ingress host.

Status: `apps/web` reads its configuration at build time (`import.meta.env`) and the repository has no Dockerfile
for the web image yet, so the web app needs that image, built to read `window.__LOREBASE_CONFIG__`, before
this component is usable. Everything else works without it.

## Observability

`telemetry.otlpEndpoint` sets the OTLP/gRPC collector; sampling is `telemetry.sampleRatio`. Keeper, Scribe and Herald
export OTLP and do not serve `/metrics`, so `serviceMonitor` scrapes the Prometheus exporter of your collector
(`deploy/observability/otel-collector.yaml`, port 8889), selected by `serviceMonitor.selector`. Logs are JSON on stdout.

## Backup and restore

Everything is in Postgres plus the optional blob store, so backup is Postgres backup: PITR with pgBackRest, WAL-G or
your managed service's snapshots (design 15.3; promoting a replica needs only a DSN change). Domain level
export, import and restore bundles, garbage collection, and the `pg_dump` of the installation-wide tables are in
[operations.md](operations.md). Take a backup before an upgrade that carries a migration. The chart manages no
backup job for an external database; with the CloudNativePG subchart, configure its backup options through the
`postgresql` values (see its documentation).

## Validation status

The chart is checked by `helm lint --strict`, `helm template` of every profile and `kubeconform --strict` in the
`helm` job of CI. It has not been installed on a live cluster in this change: the pieces that depend on a running
cluster (the hook ordering, the Ollama sidecar, the CloudNativePG integration, NetworkPolicy behavior) follow the
documented Kubernetes and Helm semantics but are untested end to end. The web image and a metrics endpoint
on the services do not exist yet; the chart's switches for them are off. Scribe is off by default.
