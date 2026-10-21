"""Structured Sources (design 9.1): connectors, row mapping, delta and the synchronization run."""

from lorebase_scribe.sources.connectors import (
    Connector,
    ConnectorFactory,
    CsvConnector,
    HttpJsonConnector,
    JsonConnector,
    JsonLinesConnector,
    ParquetConnector,
    PostgresConnector,
    SourceReadError,
    default_connector,
)
from lorebase_scribe.sources.delta import CurrentState, Delta, StoredRecord, compute_delta
from lorebase_scribe.sources.mapping import (
    MappedRow,
    RowError,
    RowMapper,
    SourceDefinitionError,
)
from lorebase_scribe.sources.profile import profile_source
from lorebase_scribe.sources.sync import (
    PERMANENT_ERRORS,
    SourceNotFoundError,
    SourceSync,
    SourceSyncError,
    SyncReport,
)

__all__ = [
    "PERMANENT_ERRORS",
    "Connector",
    "ConnectorFactory",
    "CsvConnector",
    "CurrentState",
    "Delta",
    "HttpJsonConnector",
    "JsonConnector",
    "JsonLinesConnector",
    "MappedRow",
    "ParquetConnector",
    "PostgresConnector",
    "RowError",
    "RowMapper",
    "SourceDefinitionError",
    "SourceNotFoundError",
    "SourceReadError",
    "SourceSync",
    "SourceSyncError",
    "StoredRecord",
    "SyncReport",
    "compute_delta",
    "default_connector",
    "profile_source",
]
