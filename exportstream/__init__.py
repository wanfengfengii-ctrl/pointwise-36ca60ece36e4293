"""Streaming, projection-only reader for JSON exports.

``rows(chunks, fields)`` consumes a single bytes stream chunk by chunk and
yields one small dict per record of the top-level ``records`` array, without
ever holding the whole document or the skipped parts of it in memory.
"""

from ._stream import MAX_DEPTH, MAX_FIELDS, MAX_PROJECTION, rows

__all__ = ["rows", "MAX_DEPTH", "MAX_FIELDS", "MAX_PROJECTION"]
