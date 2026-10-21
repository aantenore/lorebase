"""The `scribe` settings section: worker loop, scheduler and Source synchronization.

Every value has a documented default (see `config/lorebase.example.yaml`); deployments override
them in the YAML file or with `LOREBASE__SCRIBE__...` variables. Job retries and stale locks
come from `store.jobs`, webhook delivery from `webhooks`, garbage collection from `store.gc`.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, field_validator, model_validator

from lorebase_platform.settings import SectionModel, Settings, register_section
from lorebase_scribe.cron import CronError, parse_cron
from lorebase_scribe.extraction.settings import ExtractionSettings

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


class SourceSettings(SectionModel):
    """Source synchronization (design 9.1)."""

    allowed_connections: tuple[str, ...] = Field(
        default=(),
        description="Secret references (secret://env/NAME, secret://file/PATH) a Source "
        "`connection` may name. Sources are defined by schema authors, so any other reference "
        "is refused rather than resolved on this host. Empty: no Source can be synchronized.",
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
    postgres: PostgresSourceSettings = Field(default_factory=PostgresSourceSettings)


class ScribeSettings(SectionModel):
    """Scribe, the ingestion and maintenance worker."""

    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    sources: SourceSettings = Field(default_factory=SourceSettings)
    extraction: ExtractionSettings = Field(default_factory=ExtractionSettings)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.worker.heartbeat_interval_s <= self.worker.poll_interval_s:
            raise ValueError("worker.heartbeat_interval_s must exceed worker.poll_interval_s")
        return self


register_section(SECTION, ScribeSettings)


def scribe_settings(settings: Settings) -> ScribeSettings:
    """The validated `scribe` section."""
    return settings.section(SECTION, ScribeSettings)
