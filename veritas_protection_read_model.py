"""Compatibility names for the single protective I/O implementation.

Existing callers share the same projection, transaction behavior and optional
per-position recovery as the canonical guard. No second writer is maintained.
"""
from veritas_protective_io import (
    BATCH_SIZE,
    POSITION_COLUMNS,
    PROTECTION_FIELDS,
    PROTECTION_POSITIONS_SQL as PROTECTION_SQL,
    position_sql,
    write_patches,
)
