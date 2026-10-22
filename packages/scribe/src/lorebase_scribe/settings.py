"""The `scribe` settings section: worker loop, scheduler and Source synchronization.

Every value has a documented default (see `config/lorebase.example.yaml`); deployments override
them in the YAML file or with `LOREBASE__SCRIBE__...` variables. Job retries and stale locks
come from `store.jobs`, webhook delivery from `webhooks`, garbage collection from `store.gc`.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from lorebase_platform.secrets import SecretValue
from lorebase_platform.settings import SectionModel, Settings, register_section
from lorebase_scribe.cron import CronError, parse_cron
from lorebase_scribe.extraction.settings import ExtractionSettings
from lorebase_store.auth.checkpoints import CheckpointError, load_private_key

SECTION = "scribe"


class WorkerSettings(SectionModel):
    """The job loop of one Scribe process."""

    concurrency: int = Field(
        default=2, ge=1, le=64, description="Jobs run at the same time by one process."
    )
    poll_interval_s: float = Field(
        default=2.0, gt=0, description="Pause of an idle slot before it looks for jobs again."
    )
    heartbeat_interval_s: float = Field(
        default=60.0,
        gt=0,
        description="Running jobs get their lock time refreshed this often; keep it well below "
        "store.jobs.stale_after_seconds.",
    )
    shutdown_grace_s: float = Field(
        default=30.0,
        ge=0,
        description="On shutdown, running jobs get this long to finish; unfinished ones go "
        "back to the queue without counting the attempt.",
    )
    statement_timeout_ms: int = Field(
        default=0,
        ge=0,
        description="statement_timeout of Scribe's own database connections; 0 disables it "
        "(collections and index builds run long). Postgres Sources use "
        "scribe.sources.postgres.statement_timeout_ms.",
    )
    principal: str = Field(
        default="svc_scribe",
        min_length=1,
        description="Principal id recorded as the author of Scribe's commits and Proposals.",
    )
    types: tuple[str, ...] = Field(
        default=(),
        description="Job types this process runs; empty runs every type Scribe knows.",
    )


class SchedulerSettings(SectionModel):
    """Periodic jobs. One Scribe process at a time is the leader (Postgres advisory lock)."""

    enabled: bool = Field(default=True, description="Take part in the scheduler election.")
    tick_s: float = Field(default=15.0, gt=0, description="Interval between scheduling passes.")
    lock_name: str = Field(
        default="lorebase.scribe.scheduler",
        min_length=1,
        description="Name hashed into the advisory lock key of the leader election.",
    )
    webhook_interval_s: float = Field(
        default=10.0,
        ge=0,
        description="Queue a webhook delivery pass this often (retries included); 0 disables.",
    )
    gc_schedule: str | None = Field(
        default="0 3 * * *",
        description="Cron expression (UTC) of the garbage collection of every Domain; null "
        "disables it.",
    )
    gc_dry_run: bool = Field(default=False, description="Scheduled collections only report.")
    source_schedules: bool = Field(
        default=True,
        description="Queue Source synchronizations according to each Source's `schedule`.",
    )
    embeddings_interval_s: float = Field(
        default=60.0,
        ge=0,
        description="Queue an embedding pass of every Domain this often; on a branch whose "
        "head did not move a pass reads its schema and one key. 0 disables it.",
    )
    audit_verify_interval_s: float = Field(
        default=86400.0,
        ge=0,
        description="Queue a verification of every audit hash chain this often; a broken chain "
        "counts in `lorebase.audit.verifications` with outcome `broken`. The pass reads the "
        "whole audit log in batches. 0 disables it.",
    )
    domain_batch: int = Field(default=100, ge=1, description="Domains read per query.")

    @field_validator("gc_schedule")
    @classmethod
    def _cron(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                parse_cron(value)
            except CronError as error:
                raise ValueError(str(error)) from None
        return value


class CsvSourceSettings(SectionModel):
    """Reading CSV Sources from local files or HTTP(S) URLs."""

    allowed_roots: tuple[str, ...] = Field(
        default=(),
        description="Directories local CSV files may be read from; empty refuses local files.",
    )
    allowed_hosts: tuple[str, ...] = Field(
        default=(),
        description="Host names CSV URLs may point at; empty refuses URLs.",
    )
    allowed_schemes: tuple[str, ...] = Field(
        default=("https",), min_length=1, description="URL schemes CSV URLs may use."
    )
    allow_private_targets: bool = Field(
        default=False,
        description="Allow CSV URL hosts that resolve to loopback, private or link-local "
        "addresses. Off by default: every resolved address must be global unicast.",
    )
    max_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=1,
        description="Larger files or downloads are refused; reads stop after this many bytes.",
    )
    timeout_s: float = Field(
        default=60.0, gt=0, description="Time budget of one download, from lookup to last byte."
    )
    encodings: tuple[str, ...] = Field(
        default=("utf-8-sig", "cp1252"),
        min_length=1,
        description="Encodings tried in order; the first that decodes the file wins.",
    )
    delimiters: str = Field(
        default=",;\t|", min_length=1, description="Delimiters the dialect sniffer may pick."
    )
    sniff_bytes: int = Field(
        default=65536, ge=1024, description="Leading bytes used to detect the delimiter."
    )


class PostgresSourceSettings(SectionModel):
    """Reading Postgres Sources: read-only transaction, bounded statements."""

    statement_timeout_ms: int = Field(
        default=60000, ge=1, description="statement_timeout of the read transaction."
    )
    connect_timeout_s: float = Field(default=10.0, gt=0, description="Connection timeout.")
    fetch_rows: int = Field(default=5000, ge=1, description="Rows fetched per round trip.")


class MysqlSourceSettings(SectionModel):
    """Reading MySQL and MariaDB Sources (needs the `mysql` extra: asyncmy): read-only
    session, bounded statements."""

    statement_timeout_ms: int = Field(
        default=60000,
        ge=1,
        description="Longest execution of the read: MAX_EXECUTION_TIME on MySQL, "
        "max_statement_time on MariaDB (whole seconds, rounded up).",
    )
    connect_timeout_s: float = Field(default=10.0, gt=0, description="Connection timeout.")
    fetch_rows: int = Field(default=5000, ge=1, description="Rows fetched per round trip.")


class MssqlSourceSettings(SectionModel):
    """Reading SQL Server Sources (needs the `mssql` extra: pymssql, run in worker threads).

    SQL Server has no read-only transaction: connect with a login that can only read."""

    statement_timeout_ms: int = Field(
        default=60000,
        ge=1,
        description="Longest wait for a result (driver query timeout, whole seconds rounded "
        "up) and for a lock (LOCK_TIMEOUT).",
    )
    connect_timeout_s: float = Field(default=10.0, gt=0, description="Login timeout.")
    fetch_rows: int = Field(default=5000, ge=1, description="Rows fetched per round trip.")


class JsonSourceSettings(SectionModel):
    """Reading `json` documents and `jsonl` (JSON Lines) files from local paths."""

    allowed_roots: tuple[str, ...] = Field(
        default=(),
        description="Directories JSON and JSON Lines files may be read from; empty refuses "
        "every file.",
    )
    max_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=1,
        description="Largest `json` document; it is parsed whole, so larger files are refused.",
    )
    max_line_bytes: int = Field(
        default=16 * 1024 * 1024,
        ge=1,
        description="Longest line of a `jsonl` file; the file itself is streamed.",
    )


class ParquetSourceSettings(SectionModel):
    """Reading `parquet` files from local paths (needs the `parquet` extra: pyarrow)."""

    allowed_roots: tuple[str, ...] = Field(
        default=(),
        description="Directories Parquet files may be read from; empty refuses every file.",
    )
    batch_rows: int = Field(
        default=10000,
        ge=1,
        description="Rows decoded per batch; a file is streamed batch by batch, never whole.",
    )


class HttpJsonSourceSettings(SectionModel):
    """Reading `http_json` Sources: GET requests to JSON APIs, page by page."""

    allowed_hosts: tuple[str, ...] = Field(
        default=(),
        description="Host names the URLs may point at, next pages included; empty refuses "
        "every URL.",
    )
    allowed_schemes: tuple[str, ...] = Field(
        default=("https",), min_length=1, description="URL schemes the URLs may use."
    )
    allow_private_targets: bool = Field(
        default=False,
        description="Allow hosts that resolve to loopback, private or link-local addresses. "
        "Off by default: every resolved address must be global unicast.",
    )
    timeout_s: float = Field(
        default=30.0, gt=0, description="Time budget of one request, from lookup to last byte."
    )
    max_bytes: int = Field(
        default=50 * 1024 * 1024,
        ge=1,
        description="Larger responses are refused; reads stop after this many bytes.",
    )
    max_pages: int = Field(
        default=1000,
        ge=1,
        description="Pages one run may read; a Source with more fails instead of committing "
        "part of it. A Source's `http.max_pages` can only lower it.",
    )
    max_attempts: int = Field(
        default=4, ge=1, description="Attempts per request, the first one included."
    )
    retry_statuses: tuple[int, ...] = Field(
        default=(429, 500, 502, 503, 504),
        description="Response statuses that are retried; network errors and timeouts are too.",
    )
    backoff_base_s: float = Field(default=1.0, ge=0, description="Pause before the first retry.")
    backoff_factor: float = Field(
        default=2.0, ge=1, description="Each further retry waits this many times longer."
    )
    backoff_max_s: float = Field(
        default=60.0,
        ge=0,
        description="Longest pause between attempts, a server's Retry-After included.",
    )


class SourceSettings(SectionModel):
    """Source synchronization (design 9.1)."""

    allowed_connections: tuple[str, ...] = Field(
        default=(),
        description="Secret references (secret://env/NAME, secret://file/PATH) a Source "
        "`connection` or an `http_json` header value may name. Sources are defined by schema "
        "authors, so any other reference is refused rather than resolved on this host. Empty: "
        "no Source can be synchronized.",
    )
    commit_batch: int = Field(
        default=5000,
        ge=1,
        description="Changes per Revision on the Source branch; the changes of one row are "
        "never split across Revisions.",
    )
    max_row_errors: int = Field(
        default=100,
        ge=0,
        description="Rows that fail mapping are skipped and reported; a run with more failed "
        "rows than this commits nothing.",
    )
    error_samples: int = Field(default=20, ge=1, description="Row errors kept in the run record.")
    auto_merge_protected: bool = Field(
        default=True,
        description="Sources with an automatic merge policy merge into protected target "
        "branches too; false leaves those Proposals open for review.",
    )
    csv: CsvSourceSettings = Field(default_factory=CsvSourceSettings)
    json_files: JsonSourceSettings = Field(default_factory=JsonSourceSettings)
    parquet: ParquetSourceSettings = Field(default_factory=ParquetSourceSettings)
    http_json: HttpJsonSourceSettings = Field(default_factory=HttpJsonSourceSettings)
    postgres: PostgresSourceSettings = Field(default_factory=PostgresSourceSettings)
    mysql: MysqlSourceSettings = Field(default_factory=MysqlSourceSettings)
    mssql: MssqlSourceSettings = Field(default_factory=MssqlSourceSettings)


class EmbeddingRetrySettings(SectionModel):
    """Retries of a failed model call inside a job, after the provider's own retries."""

    attempts: int = Field(default=2, ge=1, description="Attempts per call, the first included.")
    backoff_s: float = Field(default=2.0, ge=0, description="Pause before the first retry.")
    backoff_max_s: float = Field(
        default=60.0, ge=0, description="Longest pause; each retry waits twice as long."
    )


class EmbeddingJobSettings(SectionModel):
    """Embedding jobs (design 9.3). The model is `inference.embeddings`; Kinds opt in with
    `search.embedding_template` in the schema."""

    page_rows: int = Field(
        default=500, ge=1, description="Record rows read and rendered per query."
    )
    batch_texts: int = Field(
        default=64,
        ge=1,
        description="Texts per call of the embedding model (the provider may split a call "
        "further by inference.embeddings.batch_size).",
    )
    max_records_per_job: int = Field(
        default=20000,
        ge=1,
        description="Rows one job reads; a pass that needs more queues a continuation, so a "
        "large backfill never holds a worker slot for long.",
    )
    max_calls_per_second: float = Field(
        default=0.0,
        ge=0,
        description="Model calls per second of one job (a paid endpoint's rate limit); 0 "
        "does not limit them.",
    )
    max_text_chars: int = Field(
        default=8000,
        ge=1,
        description="Rendered texts are cut to this many characters before they are hashed and "
        "embedded.",
    )
    template_cache_size: int = Field(
        default=64,
        ge=1,
        description="Schemas whose resolved embedding templates a Scribe process keeps, by "
        "schema hash: branches sharing a schema, and passes over an unchanged one, do not read "
        "and parse it again.",
    )
    retry: EmbeddingRetrySettings = Field(default_factory=EmbeddingRetrySettings)


class CheckpointHttpSettings(SectionModel):
    """An HTTP endpoint receiving each checkpoint (a webhook, or an object store accepting a
    PUT with a bearer token)."""

    url: str | None = Field(
        default=None,
        description="Target URL; `{name}` is replaced by the checkpoint's object name "
        "(prefix, stream, time), e.g. https://audit.example.com/lorebase/{name}.",
    )
    method: Literal["PUT", "POST"] = Field(default="PUT", description="Request method.")
    headers: dict[str, SecretValue] = Field(
        default_factory=dict,
        description="Extra request headers; values may be secret references, e.g. "
        "Authorization: secret://env/LOREBASE_CHECKPOINT_TOKEN.",
    )
    allowed_schemes: tuple[str, ...] = Field(
        default=("https",), min_length=1, description="URL schemes the URL may use."
    )
    allow_private_targets: bool = Field(
        default=False,
        description="Allow a host resolving to loopback, private or link-local addresses (an "
        "in-cluster store). Off by default: every resolved address must be global unicast and "
        "the connection is pinned to the checked addresses; redirects are never followed.",
    )
    timeout_s: float = Field(default=10.0, gt=0, description="Time budget of one upload.")


class CheckpointS3Settings(SectionModel):
    """An S3-compatible object store (AWS S3, MinIO, Ceph...), written with a PUT signed with
    Signature Version 4. Turn on versioning or object lock on the bucket and give these
    credentials `PutObject` only, so that checkpoints cannot be replaced or deleted."""

    endpoint: str | None = Field(
        default=None, description="Endpoint URL, e.g. https://s3.eu-west-1.amazonaws.com."
    )
    bucket: str | None = Field(default=None, description="Bucket name.")
    region: str = Field(default="us-east-1", min_length=1, description="Signing region.")
    path_style: bool = Field(
        default=True,
        description="Address the bucket in the path (endpoint/bucket/key) rather than in the "
        "host name (bucket.endpoint/key).",
    )
    access_key_id: SecretValue | None = Field(default=None, description="Access key id.")
    secret_access_key: SecretValue | None = Field(default=None, description="Secret key.")
    session_token: SecretValue | None = Field(
        default=None, description="Session token of temporary credentials."
    )
    allowed_schemes: tuple[str, ...] = Field(
        default=("https",), min_length=1, description="URL schemes the endpoint may use."
    )
    allow_private_targets: bool = Field(
        default=False,
        description="Allow an endpoint resolving to private addresses (as for `http`).",
    )
    timeout_s: float = Field(default=10.0, gt=0, description="Time budget of one upload.")


class AuditCheckpointSettings(SectionModel):
    """Signed checkpoints of the audit chains published outside the database
    (`audit_checkpoint` jobs), against which `python -m lorebase_keeper audit verify
    --checkpoints` detects entries removed from the end of a chain, or rewritten."""

    enabled: bool = Field(default=False, description="Publish checkpoints.")
    interval_s: float = Field(
        default=3600.0,
        gt=0,
        description="Queue a checkpoint this often. Entries newer than the latest published "
        "checkpoint are not covered.",
    )
    streams: tuple[Literal["actions", "reads"], ...] = Field(
        default=("actions", "reads"),
        min_length=1,
        description="Audit streams checkpointed: actions (audit_log), reads (audit_read_log).",
    )
    signing_key: SecretValue | None = Field(
        default=None,
        description="Ed25519 private key in PEM (openssl genpkey -algorithm ed25519), usually "
        "secret://file//run/secrets/checkpoint.pem. Verifiers need only its public key.",
    )
    key_id: str = Field(
        default="main", min_length=1, description="Name of the key, recorded in checkpoints."
    )
    prefix: str = Field(
        default="lorebase-audit/",
        description="Prefix of the object names: {prefix}{stream}/{UTC time}.json.",
    )
    sink: Literal["file", "http", "s3"] = Field(
        default="file", description="Where checkpoints go: file, http or s3."
    )
    directory: str | None = Field(
        default=None,
        description="Directory of the file sink, on storage the database host cannot write "
        "(a separate volume, or a mounted object store).",
    )
    http: CheckpointHttpSettings = Field(default_factory=CheckpointHttpSettings)
    s3: CheckpointS3Settings = Field(default_factory=CheckpointS3Settings)

    @field_validator("prefix")
    @classmethod
    def _relative(cls, value: str) -> str:
        if value.startswith("/") or "\\" in value or ".." in value.split("/"):
            raise ValueError("prefix must be a relative name without '..'")
        return value

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if not self.enabled:
            return self
        missing = []
        if self.signing_key is None:
            missing.append("signing_key")
        if self.sink == "file" and not self.directory:
            missing.append("directory")
        if self.sink == "http" and not self.http.url:
            missing.append("http.url")
        if self.sink == "s3":
            for name in ("endpoint", "bucket", "access_key_id", "secret_access_key"):
                if getattr(self.s3, name) is None:
                    missing.append(f"s3.{name}")
        if missing:
            raise ValueError(f"enabled checkpoints need {', '.join(missing)}")
        if self.signing_key is not None:
            try:
                load_private_key(self.signing_key.get_secret_value())
            except CheckpointError as error:
                raise ValueError(f"signing_key: {error}") from None
        return self


class ScribeSettings(SectionModel):
    """Scribe, the ingestion and maintenance worker."""

    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    sources: SourceSettings = Field(default_factory=SourceSettings)
    extraction: ExtractionSettings = Field(default_factory=ExtractionSettings)
    embeddings: EmbeddingJobSettings = Field(default_factory=EmbeddingJobSettings)
    audit_checkpoints: AuditCheckpointSettings = Field(default_factory=AuditCheckpointSettings)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.worker.heartbeat_interval_s <= self.worker.poll_interval_s:
            raise ValueError("worker.heartbeat_interval_s must exceed worker.poll_interval_s")
        return self


register_section(SECTION, ScribeSettings)


def scribe_settings(settings: Settings) -> ScribeSettings:
    """The validated `scribe` section."""
    return settings.section(SECTION, ScribeSettings)
