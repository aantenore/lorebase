{{/* Environment, volumes and mounts specific to each component. */}}

{{- define "lorebase.keeperEnv" -}}
- name: LOREBASE__KEEPER__HOST
  value: 0.0.0.0
- name: LOREBASE__KEEPER__PORT
  value: {{ .Values.keeper.port | quote }}
- name: LOREBASE__KEEPER__READS__PREFER_REPLICA
  value: {{ .Values.keeper.preferReplica | quote }}
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
