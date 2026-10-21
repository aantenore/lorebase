"""The `scribe` settings section: worker loop and scheduler.

Every value has a documented default (see `config/lorebase.example.yaml`); deployments override
them in the YAML file or with `LOREBASE__SCRIBE__...` variables. Job retries and stale locks
come from `store.jobs`, webhook delivery from `webhooks`, garbage collection from `store.gc`.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, field_validator, model_validator

from lorebase_platform.settings import SectionModel, Settings, register_section
from lorebase_scribe.cron import CronError, parse_cron

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
        "(collections and index builds run long).",
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
    index_interval_s: float = Field(
        default=60.0,
        ge=0,
        description="Queue a pass of the per-Attribute index job runner this often; 0 disables.",
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


class ScribeSettings(SectionModel):
    """Scribe, the ingestion and maintenance worker."""

    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.worker.heartbeat_interval_s <= self.worker.poll_interval_s:
            raise ValueError("worker.heartbeat_interval_s must exceed worker.poll_interval_s")
        return self


register_section(SECTION, ScribeSettings)


def scribe_settings(settings: Settings) -> ScribeSettings:
    """The validated `scribe` section."""
    return settings.section(SECTION, ScribeSettings)
