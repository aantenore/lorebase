{{/* Environment, volumes and mounts specific to each component. */}}

{{/*
Rate limit backend of the Keepers (keeper.limits.rate.backend): keeper.rateLimitBackend when
set; otherwise postgres (buckets shared by every replica) when more than one Keeper may run,
unless config.keeper.limits.rate.backend chooses. Empty: the configuration decides.
*/}}
{{- define "lorebase.rateLimitBackend" -}}
{{- $chosen := .Values.keeper.rateLimitBackend | default "" -}}
{{- if not (has $chosen (list "" "local" "postgres")) -}}
{{- fail (printf "keeper.rateLimitBackend must be local, postgres or empty, got %q" $chosen) -}}
{{- end -}}
{{- if $chosen -}}
{{- $chosen -}}
{{- else if not (dig "keeper" "limits" "rate" "backend" "" .Values.config) -}}
{{- if or .Values.keeper.autoscaling.enabled (gt (int .Values.keeper.replicaCount) 1) -}}
postgres
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "lorebase.keeperEnv" -}}
- name: LOREBASE__KEEPER__HOST
  value: 0.0.0.0
- name: LOREBASE__KEEPER__PORT
  value: {{ .Values.keeper.port | quote }}
- name: LOREBASE__KEEPER__READS__PREFER_REPLICA
  value: {{ .Values.keeper.preferReplica | quote }}
{{- with include "lorebase.rateLimitBackend" . }}
- name: LOREBASE__KEEPER__LIMITS__RATE__BACKEND
  value: {{ . | quote }}
{{- end }}
{{- if include "lorebase.sessionsEnabled" . }}
{{- if include "lorebase.secretHas" (dict "ctx" . "name" "oidcClientSecret") }}
- name: LOREBASE__KEEPER__AUTH__SESSIONS__CLIENT_SECRET
  value: secret://env/LOREBASE_OIDC_CLIENT_SECRET
- name: LOREBASE_OIDC_CLIENT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "lorebase.secretName" . }}
      key: {{ .Values.secrets.keys.oidcClientSecret }}
{{- end }}
- name: LOREBASE__KEEPER__AUTH__SESSIONS__ENCRYPTION_KEY
  value: secret://env/LOREBASE_SESSION_KEY
- name: LOREBASE_SESSION_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "lorebase.secretName" . }}
      key: {{ .Values.secrets.keys.sessionEncryptionKey }}
{{- end }}
{{- end -}}

{{- define "lorebase.scribeEnv" -}}
{{- if and .Values.ollama.enabled .Values.ollama.wireScribe }}
- name: LLM_BASE_URL
  value: {{ printf "http://%s:%v/v1" (include "lorebase.componentName" (dict "ctx" . "component" "ollama")) .Values.ollama.port | quote }}
- name: LLM_MODEL
  value: {{ .Values.ollama.model | quote }}
{{- end }}
{{- if include "lorebase.secretHas" (dict "ctx" . "name" "llmApiKey") }}
- name: LLM_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "lorebase.secretName" . }}
      key: {{ .Values.secrets.keys.llmApiKey }}
      optional: true
{{- end }}
{{- end -}}

{{- define "lorebase.heraldEnv" -}}
- name: LOREBASE_CONFIG
  value: /etc/lorebase/lorebase.yaml
- name: LOREBASE__LOGGING__LEVEL
  value: {{ .Values.logLevel | quote }}
- name: LOREBASE__HERALD__TRANSPORT
  value: streamable_http
- name: LOREBASE__HERALD__HTTP__HOST
  value: 0.0.0.0
- name: LOREBASE__HERALD__HTTP__PORT
  value: {{ .Values.herald.port | quote }}
- name: LOREBASE__HERALD__HTTP__PATH
  value: {{ .Values.herald.path | quote }}
- name: LOREBASE_URL
  value: {{ default (include "lorebase.keeperUrl" .) .Values.herald.keeperUrl | quote }}
{{- with .Values.herald.domain }}
- name: LOREBASE_DOMAIN
  value: {{ . | quote }}
{{- end }}
{{- with .Values.herald.branch }}
- name: LOREBASE_BRANCH
  value: {{ . | quote }}
{{- end }}
{{- end -}}

{{/* Guide: listener, Keeper address and the model settings Scribe gets. It holds no Keeper credential. */}}
{{- define "lorebase.guideEnv" -}}
- name: LOREBASE_CONFIG
  value: /etc/lorebase/lorebase.yaml
- name: LOREBASE__LOGGING__LEVEL
  value: {{ .Values.logLevel | quote }}
- name: LOREBASE__GUIDE__HOST
  value: 0.0.0.0
- name: LOREBASE__GUIDE__PORT
  value: {{ .Values.guide.port | quote }}
- name: LOREBASE__GUIDE__KEEPER__BASE_URL
  value: {{ default (include "lorebase.keeperUrl" .) .Values.guide.keeperUrl | quote }}
- name: LOREBASE__GUIDE__HTTP__ALLOWED_ORIGINS
  value: {{ .Values.guide.allowedOrigins | toJson | quote }}
{{- include "lorebase.scribeEnv" . }}
{{- end -}}

{{/* The lorebase.yaml ConfigMap, mounted read-only at /etc/lorebase. */}}
{{- define "lorebase.configVolume" -}}
- name: config
  configMap:
    name: {{ printf "%s-config" (include "lorebase.fullname" .) }}
{{- end -}}

{{- define "lorebase.configMount" -}}
- name: config
  mountPath: /etc/lorebase
  readOnly: true
{{- end -}}

{{/* Web app: runtime config and nginx configuration, plus the writable paths nginx needs. */}}
{{- define "lorebase.webVolumes" -}}
- name: web-config
  configMap:
    name: {{ printf "%s-web" (include "lorebase.fullname" .) }}
- name: nginx-cache
  emptyDir: {}
{{- end -}}

{{- define "lorebase.webMounts" -}}
- name: web-config
  mountPath: /etc/nginx/conf.d/default.conf
  subPath: default.conf
  readOnly: true
- name: web-config
  mountPath: /usr/share/nginx/html/config.js
  subPath: config.js
  readOnly: true
- name: nginx-cache
  mountPath: /var/cache/nginx
{{- end -}}

{{/* Browser-facing origin of Keeper for the web app. */}}
{{- define "lorebase.webKeeperUrl" -}}
{{- if .Values.web.config.keeperBaseUrl -}}
{{- .Values.web.config.keeperBaseUrl -}}
{{- else if and .Values.ingress.enabled .Values.ingress.hosts -}}
{{- printf "%s://%s" (ternary "https" "http" (not (empty .Values.ingress.tls))) (first .Values.ingress.hosts).host -}}
{{- else -}}
{{- fail "web.config.keeperBaseUrl is required when web.enabled and the Ingress is off" -}}
{{- end -}}
{{- end -}}

{{/* Content-Security-Policy of the web app: web.security.csp when set, otherwise the default one
     with the Keeper origin (live mode) and web.security.connectSrc allowed for connections. Same
     policy as deploy/docker/web/40-lorebase-config.sh. */}}
{{- define "lorebase.webCsp" -}}
{{- if .Values.web.security.csp -}}
{{- .Values.web.security.csp -}}
{{- else -}}
{{- $connect := list "'self'" -}}
{{- if eq .Values.web.config.apiMode "live" -}}
{{- $keeper := urlParse (include "lorebase.webKeeperUrl" .) -}}
{{- if and $keeper.scheme $keeper.host -}}
{{- $connect = append $connect (printf "%s://%s" $keeper.scheme $keeper.host) -}}
{{- end -}}
{{- end -}}
{{- if .Values.web.config.guideUrl -}}
{{- $guide := urlParse .Values.web.config.guideUrl -}}
{{- if and $guide.scheme $guide.host -}}
{{- $connect = append $connect (printf "%s://%s" $guide.scheme $guide.host) -}}
{{- end -}}
{{- end -}}
{{- $connect = concat $connect .Values.web.security.connectSrc -}}
{{- printf "default-src 'self'; script-src 'self'; worker-src 'self' blob:; connect-src %s; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'" (join " " $connect) -}}
{{- end -}}
{{- end -}}

{{/* The security headers of one nginx location (indented by the caller). */}}
{{- define "lorebase.webSecurityHeaders" -}}
{{- $csp := include "lorebase.webCsp" . -}}
{{- if regexMatch "[\"$\\\\{}\n]" (print $csp .Values.web.security.hsts) -}}
{{- fail "web.security: csp, connectSrc and hsts cannot contain a double quote, $, backslash, brace or newline" -}}
{{- end -}}
add_header Content-Security-Policy {{ $csp | quote }} always;
add_header X-Content-Type-Options "nosniff" always;
add_header X-Frame-Options "DENY" always;
add_header Referrer-Policy "no-referrer" always;
{{- with .Values.web.security.hsts }}
add_header Strict-Transport-Security {{ . | quote }} always;
{{- end }}
{{- end -}}
