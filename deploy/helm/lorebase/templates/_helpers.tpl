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

{{/* ---- Secrets and database ------------------------------------------------------------ */}}

{{/* Name of the Secret holding the chart-managed or existing values. */}}
{{- define "lorebase.secretName" -}}
{{- default (include "lorebase.fullname" .) .Values.secrets.existingSecret -}}
{{- end -}}

{{/* "true" when the named entry (dbDsn, dbReadDsn, apiKeyPepper, llmApiKey) is wired into the pods. */}}
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
{{- if or (and (not .Values.postgresql.enabled) (get $s.values "dbDsn")) (get $s.values "dbReadDsn") (get $s.values "apiKeyPepper") (get $s.values "llmApiKey") (get $s.values "oidcClientSecret") (get $s.values "sessionEncryptionKey") $s.extra -}}true{{- end -}}
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

{{/*
Secret reference of the primary DSN: the CloudNativePG application Secret (key `uri`) when the
in-cluster database is on, else the chart-managed or existing Secret. `hook` selects the
hook-scoped copy that exists before the first install.
*/}}
{{- define "lorebase.dsnRef" -}}
{{- if .ctx.Values.postgresql.enabled -}}
name: {{ printf "%s-app" (include "lorebase.pgClusterName" .ctx) }}
key: uri
{{- else if .ctx.Values.secrets.existingSecret -}}
name: {{ .ctx.Values.secrets.existingSecret }}
key: {{ required "secrets.keys.dbDsn must name the DSN key of secrets.existingSecret" .ctx.Values.secrets.keys.dbDsn }}
{{- else -}}
name: {{ if .hook }}{{ printf "%s-migrate" (include "lorebase.fullname" .ctx) }}{{ else }}{{ include "lorebase.secretName" .ctx }}{{ end }}
key: {{ required "secrets.keys.dbDsn must not be empty" .ctx.Values.secrets.keys.dbDsn }}
{{- end -}}
{{- end -}}

{{/* Database environment shared by every service that talks to Postgres. */}}
{{- define "lorebase.dbEnv" -}}
- name: LOREBASE__DATABASE__DSN
  value: secret://env/LOREBASE_DB_DSN
- name: LOREBASE_DB_DSN
  valueFrom:
    secretKeyRef:
      {{- include "lorebase.dsnRef" . | nindent 6 }}
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
{{ include "lorebase.dbEnv" (dict "ctx" $ctx "hook" false) }}
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
