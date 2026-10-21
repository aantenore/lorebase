"""Profiles of Sources for schema inference (`lorebase_core.infer`), through their connectors.

Any Source a connector reads (CSV, JSON, JSON Lines, Parquet, HTTP JSON, Postgres) becomes a
`TableProfile` named after the last segment of its api name: every row is read and counted,
`store.profiling.records` decides which rows are kept, and the columns are the connector's
header followed by every other key seen. The connection rules and limits are those of a
synchronization; no watermark applies.
"""

from __future__ import annotations

from lorebase_core.infer import TableProfile
from lorebase_core.model import Source
from lorebase_scribe.settings import SourceSettings
from lorebase_scribe.sources.connectors import ConnectorFactory, default_connector
from lorebase_scribe.sources.mapping import SourceDefinitionError
from lorebase_store.profiling import RecordProfileError, RecordProfiler
from lorebase_store.settings import RecordProfilingSettings


async def profile_source(
    source: Source,
    settings: SourceSettings,
    profiling: RecordProfilingSettings,
    connectors: ConnectorFactory = default_connector,
) -> TableProfile:
    """The profile of every row of `source`."""
    connector = connectors(source, settings)
    profiler = RecordProfiler(profiling)
    name = source.api_name.rsplit(".", 1)[-1]
    try:
        async for _, row in connector.rows(None):
            profiler.add(row)
        return profiler.profile(name, connector.columns)
    except RecordProfileError as error:
        raise SourceDefinitionError(f"{name}: {error}") from None
