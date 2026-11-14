"""Shared helpers for the test modules."""

import json


def chunked(payload, size):
    """Split ``payload`` into chunks of at most ``size`` bytes."""
    for start in range(0, len(payload), size):
        yield payload[start:start + size]


def reference(payload, fields):
    """The obvious in-memory implementation the stream must agree with."""
    document = json.loads(payload)
    return [{name: record[name] for name in fields if name in record}
            for record in document["records"]]
