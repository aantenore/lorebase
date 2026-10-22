{{/*
Generic Deployment, Service, HorizontalPodAutoscaler and PodDisruptionBudget of a component.
Each component file builds a dict and calls these, so the scheduling, security and probe
settings behave the same everywhere.

Deployment input:
  ctx            root context
  component      keeper | scribe | herald | guide | web
  cfg            the component values
  image          image reference
  pullPolicy     image pull policy
  command        list, or empty for the image default
  port           container port, 0 for none
  env            rendered list of env entries (YAML text, may be empty)
  volumes        rendered extra volumes (YAML text, may be empty)
  mounts         rendered extra mounts (YAML text, may be empty)
  podSecurity    pod security context
  checksums      map of annotation -> value, used to roll pods when a ConfigMap changes
  preStop        seconds of preStop sleep, 0 for none
*/}}
{{- define "lorebase.deployment" -}}
{{- $ctx := .ctx -}}
{{- $c := .cfg -}}
{{- $scope := dict "ctx" $ctx "component" .component -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "lorebase.componentName" $scope }}
  labels:
    {{- include "lorebase.labels" $scope | nindent 4 }}
  {{- include "lorebase.commonAnnotations" $ctx | nindent 2 }}
spec:
  {{- if not (and $c.autoscaling $c.autoscaling.enabled) }}
  replicas: {{ $c.replicaCount }}
  {{- end }}
  {{- with $c.strategy }}
  strategy:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  selector:
    matchLabels:
      {{- include "lorebase.selectorLabels" $scope | nindent 6 }}
  template:
    metadata:
      {{- if or $c.podAnnotations .checksums }}
      annotations:
        {{- range $key, $value := .checksums }}
        {{ $key }}: {{ $value | quote }}
        {{- end }}
        {{- with $c.podAnnotations }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
      {{- end }}
      labels:
        {{- include "lorebase.labels" $scope | nindent 8 }}
        {{- with $c.podLabels }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
    spec:
      {{- with $ctx.Values.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      serviceAccountName: {{ include "lorebase.serviceAccountName" $ctx }}
      automountServiceAccountToken: {{ $ctx.Values.serviceAccount.automount }}
      securityContext:
        {{- toYaml .podSecurity | nindent 8 }}
      {{- with $c.priorityClassName }}
      priorityClassName: {{ . }}
      {{- end }}
      terminationGracePeriodSeconds: {{ $c.terminationGracePeriodSeconds }}
      containers:
        - name: {{ .component }}
          image: {{ .image | quote }}
          imagePullPolicy: {{ .pullPolicy }}
          {{- with .command }}
          command:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          securityContext:
            {{- toYaml $ctx.Values.containerSecurityContext | nindent 12 }}
          {{- if .port }}
          ports:
            - name: http
              containerPort: {{ .port }}
              protocol: TCP
          {{- end }}
          {{- if or .env $c.extraEnv }}
          env:
            {{- with .env }}
            {{- . | nindent 12 }}
            {{- end }}
            {{- with $c.extraEnv }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
          {{- end }}
          {{- with $c.startupProbe }}
          startupProbe:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- with $c.livenessProbe }}
          livenessProbe:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- with $c.readinessProbe }}
          readinessProbe:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- if .preStop }}
          lifecycle:
            preStop:
              exec:
                command: ["sleep", {{ .preStop | quote }}]
          {{- end }}
          {{- with $c.resources }}
          resources:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          volumeMounts:
            - name: tmp
              mountPath: /tmp
            {{- with .mounts }}
            {{- . | nindent 12 }}
            {{- end }}
            {{- with $c.extraVolumeMounts }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
      volumes:
        - name: tmp
          emptyDir: {}
        {{- with .volumes }}
        {{- . | nindent 8 }}
        {{- end }}
        {{- with $c.extraVolumes }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
      {{- with $c.topologySpreadConstraints }}
      topologySpreadConstraints:
        {{- include "lorebase.topologySpread" (dict "ctx" $ctx "component" $.component "constraints" .) | nindent 8 }}
      {{- end }}
      {{- with $c.affinity }}
      affinity:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $c.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $c.tolerations }}
      tolerations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
{{- end -}}

{{/* Service input: ctx, component, cfg (with .service), port (container port name http). */}}
{{- define "lorebase.service" -}}
{{- $scope := dict "ctx" .ctx "component" .component -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ include "lorebase.componentName" $scope }}
  labels:
    {{- include "lorebase.labels" $scope | nindent 4 }}
  {{- if or .cfg.service.annotations .ctx.Values.commonAnnotations }}
  annotations:
    {{- toYaml (merge (deepCopy .cfg.service.annotations) (deepCopy .ctx.Values.commonAnnotations)) | nindent 4 }}
  {{- end }}
spec:
  type: {{ .cfg.service.type }}
  ports:
    - name: http
      port: {{ .cfg.service.port }}
      targetPort: http
      protocol: TCP
  selector:
    {{- include "lorebase.selectorLabels" $scope | nindent 4 }}
{{- end -}}

{{/* HPA input: ctx, component, cfg (with .autoscaling). */}}
{{- define "lorebase.hpa" -}}
{{- $scope := dict "ctx" .ctx "component" .component -}}
{{- $a := .cfg.autoscaling -}}
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ include "lorebase.componentName" $scope }}
  labels:
    {{- include "lorebase.labels" $scope | nindent 4 }}
  {{- include "lorebase.commonAnnotations" .ctx | nindent 2 }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ include "lorebase.componentName" $scope }}
  minReplicas: {{ $a.minReplicas }}
  maxReplicas: {{ $a.maxReplicas }}
  metrics:
    {{- with $a.targetCPUUtilizationPercentage }}
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ . }}
    {{- end }}
    {{- with $a.targetMemoryUtilizationPercentage }}
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: {{ . }}
    {{- end }}
    {{- with $a.customMetrics }}
    {{- toYaml . | nindent 4 }}
    {{- end }}
  {{- with $a.behavior }}
  behavior:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- end -}}

{{/* PDB input: ctx, component, cfg (with .pdb). */}}
{{- define "lorebase.pdb" -}}
{{- $scope := dict "ctx" .ctx "component" .component -}}
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ include "lorebase.componentName" $scope }}
  labels:
    {{- include "lorebase.labels" $scope | nindent 4 }}
  {{- include "lorebase.commonAnnotations" .ctx | nindent 2 }}
spec:
  {{- if not (kindIs "invalid" .cfg.pdb.minAvailable) }}
  minAvailable: {{ .cfg.pdb.minAvailable }}
  {{- end }}
  {{- if not (kindIs "invalid" .cfg.pdb.maxUnavailable) }}
  maxUnavailable: {{ .cfg.pdb.maxUnavailable }}
  {{- end }}
  selector:
    matchLabels:
      {{- include "lorebase.selectorLabels" $scope | nindent 6 }}
{{- end -}}
