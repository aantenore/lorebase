{{/*
Names, labels and shared fragments. Every helper that is specific to a component takes a dict
with `ctx` (the root context) and `component`.
*/}}

{{- define "lorebase.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "lorebase.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "lorebase.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Resource name of a component: <fullname>-<component>. */}}
{{- define "lorebase.componentName" -}}
{{- printf "%s-%s" (include "lorebase.fullname" .ctx) .component | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Labels that select the pods of a component. */}}
{{- define "lorebase.selectorLabels" -}}
app.kubernetes.io/name: {{ include "lorebase.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* All labels of a resource of a component. */}}
{{- define "lorebase.labels" -}}
helm.sh/chart: {{ include "lorebase.chart" .ctx }}
{{ include "lorebase.selectorLabels" . }}
app.kubernetes.io/version: {{ .ctx.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .ctx.Release.Service }}
app.kubernetes.io/part-of: lorebase
{{- with .ctx.Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* Annotations shared by every resource (empty output when none). */}}
{{- define "lorebase.commonAnnotations" -}}
{{- with .Values.commonAnnotations }}
annotations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{- define "lorebase.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "lorebase.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* Image reference. A tag that starts with @ is a digest. */}}
{{- define "lorebase.image" -}}
{{- $tag := default .ctx.Chart.AppVersion .image.tag | toString -}}
{{- if hasPrefix "@" $tag -}}
{{- printf "%s%s" .image.repository $tag -}}
{{- else -}}
{{- printf "%s:%s" .image.repository $tag -}}
{{- end -}}
{{- end -}}

{{/* Backend image of a component (Keeper, Scribe, Herald, migration). */}}
{{- define "lorebase.backendImage" -}}
{{- include "lorebase.image" (dict "ctx" . "image" .Values.image) -}}
{{- end -}}

{{/* Image of Scribe and Guide: imageMl, or the backend image when imageMl.repository is empty. */}}
{{- define "lorebase.mlImage" -}}
{{- if .Values.imageMl.repository -}}
{{- include "lorebase.image" (dict "ctx" . "image" (dict "repository" .Values.imageMl.repository "tag" (default .Values.image.tag .Values.imageMl.tag))) -}}
{{- else -}}
{{- include "lorebase.backendImage" . -}}
{{- end -}}
{{- end -}}

{{/* ---- Secrets and database ------------------------------------------------------------ */}}

{{/* Name of the Secret holding the chart-managed or existing values. */}}
{{- define "lorebase.secretName" -}}
{{- default (include "lorebase.fullname" .) .Values.secrets.existingSecret -}}
{{- end -}}

{{/* "true" when the named entry (dbDsn, dbReadDsn, apiKeyPepper, llmApiKey, ...) is wired into the pods. */}}
{{- define "lorebase.secretHas" -}}
{{- $s := .ctx.Values.secrets -}}
{{- $key := get $s.keys .name -}}
{{- if $key -}}
{{- if $s.existingSecret -}}
{{- /* An existing Secret is trusted to hold the key; the pepper is wired only where API keys are used. */ -}}
{{- if eq .name "apiKeyPepper" -}}{{- if contains "api_key" (dig "keeper" "auth" "mode" "none" .ctx.Values.config) -}}true{{- end -}}
{{- else if or (eq .name "oidcClientSecret") (eq .name "sessionEncryptionKey") -}}{{- if include "lorebase.sessionsEnabled" .ctx -}}true{{- end -}}
{{- else -}}true{{- end -}}
{{- else if get $s.values .name -}}{{- if or (not (or (eq .name "oidcClientSecret") (eq .name "sessionEncryptionKey"))) (include "lorebase.sessionsEnabled" .ctx) -}}true{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* "true" when browser sign-in (config.keeper.auth.sessions) is enabled. */}}
{{- define "lorebase.sessionsEnabled" -}}
{{- if dig "keeper" "auth" "sessions" "enabled" false .Values.config -}}true{{- end -}}
{{- end -}}

{{/* "true" when the chart must create the managed Secret. */}}
{{- define "lorebase.managedSecret" -}}
{{- $s := .Values.secrets -}}
{{- if not $s.existingSecret -}}
{{- if or (and (not .Values.postgresql.enabled) (or (get $s.values "dbDsn") (get $s.values "dbMaintenanceDsn"))) (get $s.values "dbReadDsn") (get $s.values "apiKeyPepper") (get $s.values "llmApiKey") (get $s.values "oidcClientSecret") (get $s.values "sessionEncryptionKey") (get $s.values "webhookEncryptionKey") $s.extra -}}true{{- end -}}
{{- end -}}
{{- end -}}

{{/* Name of the CloudNativePG Cluster (same rule as the cluster chart; under the alias its chart name is "postgresql"). */}}
{{- define "lorebase.pgClusterName" -}}
{{- if .Values.postgresql.fullnameOverride -}}
{{- .Values.postgresql.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if contains "postgresql" .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-postgresql" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* "true" when the chart creates the basic-auth Secret of the application role (in-cluster Postgres; also with a shared role, so the operator can create the role before the switch). */}}
{{- define "lorebase.appRoleSecret" -}}
{{- if and .Values.postgresql.enabled (not .Values.secrets.existingSecret) (get .Values.secrets.values "dbAppPassword") -}}true{{- end -}}
{{- end -}}

{{/* "true" when the `pg-roles` hook declares the in-cluster roles before the migration of an upgrade. */}}
{{- define "lorebase.clusterRolesHook" -}}
{{- $setup := .Values.database.setup -}}
{{- if and .Values.postgresql.enabled (include "lorebase.splitRoles" .) .Values.migrate.enabled $setup.enabled $setup.clusterRoles.enabled (has "pre-upgrade" .Values.migrate.hookEventsInCluster) -}}true{{- end -}}
{{- end -}}

{{/*
"true" when Keeper and Scribe connect as the application role and the migration as the maintenance
role (the default). Empty with `database.allowSharedRole`, where every component uses one DSN.
*/}}
{{- define "lorebase.splitRoles" -}}
{{- if not .Values.database.allowSharedRole -}}true{{- end -}}
{{- end -}}

{{/* Secret holding the DSNs of an external database; `hook` selects the hook-scoped copy that exists before the first install. */}}
{{- define "lorebase.dbSecretName" -}}
{{- if .ctx.Values.secrets.existingSecret -}}
{{- .ctx.Values.secrets.existingSecret -}}
{{- else if .hook -}}
{{- printf "%s-migrate" (include "lorebase.fullname" .ctx) -}}
{{- else -}}
{{- include "lorebase.secretName" .ctx -}}
{{- end -}}
{{- end -}}

{{/*
Secret reference of the maintenance DSN (the owner of the tables): the CloudNativePG application
Secret (key `uri`, the database owner) with the in-cluster database, else `secrets.keys.dbMaintenanceDsn`
(`secrets.keys.dbDsn` with a shared role).
*/}}
{{- define "lorebase.maintenanceDsnRef" -}}
{{- if .ctx.Values.postgresql.enabled -}}
name: {{ printf "%s-app" (include "lorebase.pgClusterName" .ctx) }}
key: uri
{{- else -}}
name: {{ include "lorebase.dbSecretName" . }}
{{- if include "lorebase.splitRoles" .ctx }}
key: {{ required "secrets.keys.dbMaintenanceDsn must not be empty" .ctx.Values.secrets.keys.dbMaintenanceDsn }}
{{- else }}
key: {{ required "secrets.keys.dbDsn must not be empty" .ctx.Values.secrets.keys.dbDsn }}
{{- end }}
{{- end -}}
{{- end -}}

{{/*
Environment variable `name` holding the DSN of the application role. In-cluster: built from the role
name, the password of its basic-auth Secret and the `-rw` Service (the owner Secret with a shared
role). External: `secrets.keys.dbDsn`.
*/}}
{{- define "lorebase.appDsnEnv" -}}
{{- $ctx := .ctx -}}
{{- $pg := $ctx.Values.postgresql -}}
{{- if and $pg.enabled (include "lorebase.splitRoles" $ctx) -}}
- name: LOREBASE_DB_APP_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ required "database.appRole.passwordSecret must not be empty" $ctx.Values.database.appRole.passwordSecret }}
      key: password
      {{- if .hook }}
      {{- /* An upgrade creates the Secret after its hooks; the role setup needs only the user name. */}}
      optional: true
      {{- end }}
- name: {{ .name }}
  value: {{ printf "postgresql+asyncpg://%s:$(LOREBASE_DB_APP_PASSWORD)@%s-rw:5432/%s" $ctx.Values.database.appRole.name (include "lorebase.pgClusterName" $ctx) $pg.cluster.initdb.database | quote }}
{{- else -}}
- name: {{ .name }}
  valueFrom:
    secretKeyRef:
      {{- if $pg.enabled }}
      name: {{ printf "%s-app" (include "lorebase.pgClusterName" $ctx) }}
      key: uri
      {{- else }}
      name: {{ include "lorebase.dbSecretName" . }}
      key: {{ required "secrets.keys.dbDsn must not be empty" $ctx.Values.secrets.keys.dbDsn }}
      {{- end }}
{{- end -}}
{{- end -}}

{{/* Database environment of Keeper and Scribe: the application role, and the maintenance role for index DDL when `maintenance`. */}}
{{- define "lorebase.dbEnv" -}}
- name: LOREBASE__DATABASE__DSN
  value: secret://env/LOREBASE_DB_DSN
{{ include "lorebase.appDsnEnv" (dict "ctx" .ctx "hook" false "name" "LOREBASE_DB_DSN") }}
{{- if and .maintenance (include "lorebase.splitRoles" .ctx) }}
- name: LOREBASE__DATABASE__MAINTENANCE_DSN
  value: secret://env/LOREBASE_DB_MAINTENANCE_DSN
- name: LOREBASE_DB_MAINTENANCE_DSN
  valueFrom:
    secretKeyRef:
      {{- include "lorebase.maintenanceDsnRef" (dict "ctx" .ctx "hook" false) | nindent 6 }}
{{- end }}
{{- end -}}

{{/* Database environment of the migration Job: the maintenance role, and the application role it grants to. */}}
{{- define "lorebase.migrateDbEnv" -}}
- name: LOREBASE__DATABASE__DSN
  value: secret://env/LOREBASE_DB_DSN
- name: LOREBASE_DB_DSN
  valueFrom:
    secretKeyRef:
      {{- include "lorebase.maintenanceDsnRef" (dict "ctx" . "hook" true) | nindent 6 }}
{{- if include "lorebase.splitRoles" . }}
- name: LOREBASE__DATABASE__APPLICATION_ROLE
  value: {{ .Values.database.appRole.name | quote }}
{{- end }}
{{- end -}}

{{/* Environment shared by Keeper and Scribe. */}}
{{- define "lorebase.appEnv" -}}
{{- $ctx := .ctx -}}
- name: LOREBASE_CONFIG
  value: /etc/lorebase/lorebase.yaml
- name: LOREBASE__LOGGING__LEVEL
  value: {{ $ctx.Values.logLevel | quote }}
- name: LOREBASE__TELEMETRY__SERVICE_NAME
  value: {{ printf "lorebase-%s" .component | quote }}
{{- with $ctx.Values.telemetry.otlpEndpoint }}
- name: LOREBASE__TELEMETRY__OTLP_ENDPOINT
  value: {{ . | quote }}
- name: LOREBASE__TELEMETRY__SAMPLE_RATIO
  value: {{ $ctx.Values.telemetry.sampleRatio | quote }}
{{- end }}
{{ include "lorebase.dbEnv" (dict "ctx" $ctx "maintenance" (or (eq .component "scribe") $ctx.Values.database.maintenanceDsnInKeeper)) }}
{{- if include "lorebase.secretHas" (dict "ctx" $ctx "name" "dbReadDsn") }}
- name: LOREBASE_DB_READ_DSN
  valueFrom:
    secretKeyRef:
      name: {{ include "lorebase.secretName" $ctx }}
      key: {{ $ctx.Values.secrets.keys.dbReadDsn }}
      optional: true
{{- end }}
{{- if include "lorebase.secretHas" (dict "ctx" $ctx "name" "apiKeyPepper") }}
- name: LOREBASE__KEEPER__AUTH__API_KEYS__PEPPER
  value: secret://env/LOREBASE_API_KEY_PEPPER
- name: LOREBASE_API_KEY_PEPPER
  valueFrom:
    secretKeyRef:
      name: {{ include "lorebase.secretName" $ctx }}
      key: {{ $ctx.Values.secrets.keys.apiKeyPepper }}
{{- end }}
{{- if include "lorebase.secretHas" (dict "ctx" $ctx "name" "webhookEncryptionKey") }}
- name: LOREBASE__WEBHOOKS__ENCRYPTION_KEY
  value: secret://env/LOREBASE_WEBHOOK_KEY
- name: LOREBASE_WEBHOOK_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "lorebase.secretName" $ctx }}
      key: {{ $ctx.Values.secrets.keys.webhookEncryptionKey }}
{{- end }}
{{- $extra := list -}}
{{- if $ctx.Values.secrets.existingSecret -}}
{{- $extra = $ctx.Values.secrets.extraNames -}}
{{- else -}}
{{- $extra = keys $ctx.Values.secrets.extra | sortAlpha -}}
{{- end }}
{{- range $extra }}
- name: {{ . | quote }}
  valueFrom:
    secretKeyRef:
      name: {{ include "lorebase.secretName" $ctx }}
      key: {{ . | quote }}
{{- end }}
{{- end -}}

{{/* Rendered lorebase.yaml of the application. */}}
{{- define "lorebase.appConfig" -}}
{{- toYaml .Values.config -}}
{{- end -}}

{{/* Subset of the settings that the migration needs (database, store layout, logging). */}}
{{- define "lorebase.migrateConfig" -}}
{{- toYaml (pick .Values.config "database" "store" "logging") -}}
{{- end -}}

{{/* Helm hook events of the migration Job and its hook resources. */}}
{{- define "lorebase.hookEvents" -}}
{{- $events := ternary .Values.migrate.hookEventsInCluster .Values.migrate.hookEvents .Values.postgresql.enabled -}}
{{- join "," $events -}}
{{- end -}}

{{/* In-cluster URL of Keeper. */}}
{{- define "lorebase.keeperUrl" -}}
{{- printf "http://%s:%v" (include "lorebase.componentName" (dict "ctx" . "component" "keeper")) .Values.keeper.service.port -}}
{{- end -}}

{{/* ---- Scheduling ------------------------------------------------------------------------ */}}

{{/* Topology spread constraints with the selector of the component added when omitted. */}}
{{- define "lorebase.topologySpread" -}}
{{- $selector := dict "matchLabels" (include "lorebase.selectorLabels" . | fromYaml) -}}
{{- $out := list -}}
{{- range .constraints -}}
{{- $out = append $out (merge (deepCopy .) (dict "labelSelector" $selector)) -}}
{{- end -}}
{{- toYaml $out -}}
{{- end -}}

{{/* ---- Validation ------------------------------------------------------------------------ */}}

{{- define "lorebase.validate" -}}
{{- $mode := dig "keeper" "auth" "mode" "none" .Values.config -}}
{{- if and (contains "api_key" $mode) (not (include "lorebase.secretHas" (dict "ctx" . "name" "apiKeyPepper"))) -}}
{{- fail (printf "config.keeper.auth.mode is %q: set secrets.values.apiKeyPepper (32 characters or more), or secrets.existingSecret with secrets.keys.apiKeyPepper" $mode) -}}
{{- end -}}
{{- if and (not .Values.postgresql.enabled) (not (include "lorebase.secretHas" (dict "ctx" . "name" "dbDsn"))) -}}
{{- fail "no database: set secrets.values.dbDsn, or secrets.existingSecret with secrets.keys.dbDsn, or postgresql.enabled=true" -}}
{{- end -}}
{{- include "lorebase.validateRoles" . -}}
{{- $webhooks := dig "webhooks" dict .Values.config -}}
{{- if not (or (include "lorebase.secretHas" (dict "ctx" . "name" "webhookEncryptionKey")) (get $webhooks "encryption_key") (get $webhooks "allow_unsealed_keys")) -}}
{{- fail "webhook signing keys are sealed: set secrets.values.webhookEncryptionKey (32 characters or more, for example openssl rand -hex 32), or secrets.existingSecret with secrets.keys.webhookEncryptionKey (config.webhooks.allow_unsealed_keys: true skips it, local development only)" -}}
{{- end -}}
{{- if include "lorebase.sessionsEnabled" . -}}
{{- $sessions := dig "keeper" "auth" "sessions" dict .Values.config -}}
{{- if not (contains "oidc" $mode) -}}
{{- fail (printf "config.keeper.auth.sessions.enabled needs an oidc auth mode (oidc or api_key+oidc), got %q" $mode) -}}
{{- end -}}
{{- range $name := list "client_id" "redirect_uri" "app_url" -}}
{{- if not (get $sessions $name) -}}
{{- fail (printf "config.keeper.auth.sessions.%s is required when sessions are enabled" $name) -}}
{{- end -}}
{{- end -}}
{{- if not (include "lorebase.secretHas" (dict "ctx" . "name" "sessionEncryptionKey")) -}}
{{- fail "sessions are enabled: set secrets.values.sessionEncryptionKey (32 characters or more), or secrets.existingSecret with secrets.keys.sessionEncryptionKey" -}}
{{- end -}}
{{- if and (ne (get $sessions "token_endpoint_auth_method" | default "client_secret_basic") "none") (not (include "lorebase.secretHas" (dict "ctx" . "name" "oidcClientSecret"))) -}}
{{- fail "sessions are enabled: set secrets.values.oidcClientSecret, or secrets.existingSecret with secrets.keys.oidcClientSecret (or config.keeper.auth.sessions.token_endpoint_auth_method: none for a public client)" -}}
{{- end -}}
{{- end -}}
{{- if and (eq .Values.web.config.authMode "session") (not (include "lorebase.sessionsEnabled" .)) -}}
{{- fail "web.config.authMode is session: enable config.keeper.auth.sessions" -}}
{{- end -}}
{{- if and .Values.postgresql.enabled (not .Values.postgresql.cluster.imageName) -}}
{{- fail "postgresql.cluster.imageName must name a CloudNativePG compatible Postgres image that includes pgvector" -}}
{{- end -}}
{{- if and .Values.ollama.enabled (not .Values.scribe.enabled) -}}
{{- fail "ollama.enabled needs scribe.enabled: Scribe is the only service that calls a model" -}}
{{- end -}}
{{- if and .Values.serviceMonitor.enabled (not .Values.serviceMonitor.selector.matchLabels) -}}
{{- fail "serviceMonitor.selector.matchLabels must select the Service of the OpenTelemetry Collector" -}}
{{- end -}}
{{- end -}}

{{/* Database roles: two roles unless `database.allowSharedRole`, the application role safe to put in a URL. */}}
{{- define "lorebase.validateRoles" -}}
{{- $role := .Values.database.appRole -}}
{{- if not (regexMatch "^[a-z_][a-z0-9_]{0,62}$" (toString $role.name)) -}}
{{- fail (printf "database.appRole.name %q: lower case letters, digits and _ only" (toString $role.name)) -}}
{{- end -}}
{{- if include "lorebase.splitRoles" . -}}
{{- $shared := "the application role (Keeper, Scribe) and the maintenance role (migrations, owner of the tables) must differ; set database.allowSharedRole=true only for development" -}}
{{- if .Values.postgresql.enabled -}}
{{- $cluster := .Values.postgresql.cluster -}}
{{- if eq $role.name (dig "initdb" "owner" "" $cluster) -}}
{{- fail (printf "database.appRole.name is the database owner %q: %s" $role.name $shared) -}}
{{- end -}}
{{- $managed := dict -}}
{{- range (default list $cluster.roles) -}}
{{- if eq (toString .name) $role.name -}}{{- $managed = . -}}{{- end -}}
{{- end -}}
{{- if not $managed -}}
{{- fail (printf "postgresql.cluster.roles must declare the application role %q (database.appRole.name)" $role.name) -}}
{{- end -}}
{{- if or $managed.superuser $managed.bypassrls (ne (dig "passwordSecret" "name" "" $managed) $role.passwordSecret) -}}
{{- fail (printf "postgresql.cluster.roles entry %q: superuser and bypassrls must be false and passwordSecret.name must be database.appRole.passwordSecret (%q)" $role.name $role.passwordSecret) -}}
{{- end -}}
{{- $password := get .Values.secrets.values "dbAppPassword" -}}
{{- if and (not .Values.secrets.existingSecret) (not $password) -}}
{{- fail (printf "the in-cluster database needs the application role password: set secrets.values.dbAppPassword, or secrets.existingSecret and create the basic-auth Secret %q (username, password)" $role.passwordSecret) -}}
{{- end -}}
{{- if and $password (not (regexMatch "^[A-Za-z0-9._~-]{16,}$" $password)) -}}
{{- fail "secrets.values.dbAppPassword: 16 or more URL-safe characters (letters, digits, . _ ~ -)" -}}
{{- end -}}
{{- else -}}
{{- if not (include "lorebase.secretHas" (dict "ctx" . "name" "dbMaintenanceDsn")) -}}
{{- fail "no maintenance role: set secrets.values.dbMaintenanceDsn (owner of the tables, runs the migrations), or secrets.existingSecret with secrets.keys.dbMaintenanceDsn; or database.allowSharedRole=true for development" -}}
{{- end -}}
{{- if not .Values.secrets.existingSecret -}}
{{- $app := get .Values.secrets.values "dbDsn" -}}
{{- $owner := get .Values.secrets.values "dbMaintenanceDsn" -}}
{{- if and $app $owner -}}
{{- $appUser := (urlParse $app).userinfo | splitList ":" | first -}}
{{- $ownerUser := (urlParse $owner).userinfo | splitList ":" | first -}}
{{- if or (eq $app $owner) (eq $appUser $ownerUser) -}}
{{- fail (printf "secrets.values.dbDsn and dbMaintenanceDsn both connect as %q: %s" $appUser $shared) -}}
{{- end -}}
{{- if ne $appUser $role.name -}}
{{- fail (printf "secrets.values.dbDsn connects as %q, database.appRole.name is %q" $appUser $role.name) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
