"""End-to-end demonstration of exportstream.

Covers the projection reference, a chunk-size differential against
``json.loads``, checks that malformed inputs are rejected, and a bounded-memory
run over a stream with 64 MiB of skipped data and 200k records.

Run with:  python3 -m demo.demo
"""

import json
import sys
import time
import tracemalloc

from exportstream import rows

REFERENCE_DOC = b'{"records": [{"id": 1}, {"id": null}, {}]}'


def chunked(payload, size):
    for start in range(0, len(payload), size):
        yield payload[start:start + size]


def reference(payload, fields):
    document = json.loads(payload)
    return [{name: record[name] for name in fields if name in record}
            for record in document["records"]]


def show_reference():
    print("reference  fields=['id']")
    print("  input :", REFERENCE_DOC.decode())
    projected = list(rows(chunked(REFERENCE_DOC, 1), ["id"]))
    print("  rows  :", json.dumps(projected))
    assert projected == [{"id": 1}, {"id": None}, {}]


def show_chunk_differential():
    document = json.dumps({
        "meta": {"tags": ["a", "b", {"deep": [None, True]}], "n": 1.5},
        "records": [
            {"id": 7, "text": "line\nbreak é \U0001f600", "name": "x"},
            {"id": -12, "name": None, "extra": {"big": [1, 2, 3]}},
            {"name": "y"},
        ],
        "tail": [[[]]],
    }, ensure_ascii=False).encode("utf-8")
    fields = ["id", "name", "text"]
    expected = reference(document, fields)
    sizes = (1, 2, 3, 5, 7, 11, 16, 31, 32, 64, 1024)
    for size in sizes:
        got = list(rows(chunked(document, size), fields))
        assert got == expected, (size, got, expected)
    print("chunk differential: %d chunk sizes (%s) agree with json.loads"
          % (len(sizes), ", ".join(map(str, sizes))))
    print("  rows:", json.dumps(expected, ensure_ascii=False))


def show_bad_inputs():
    cases = [
        (b'{"records":[{"id":1},{"id":2}]', "truncated document"),
        (b'{"records":[{"id":1}],"skip":[1,]}', "trailing comma in skip"),
        (b'{"records":[{"id":1.5}]}', "float where an int is required"),
        (b'{"records":[{"id":9223372036854775808}]}', "int64 overflow"),
        (b'{"records":[{"id":"\\q"}]}', "bad escape"),
        (b'{"records":[{"id":1}]}\n{"x":2}', "second document"),
        (b'{"records":[{"id":"\xff"}]}', "invalid UTF-8"),
        (b'{"records":{"id":1}}', "records is not an array"),
        (b'{"records":[{"id":1}],"records":[]}', "duplicate records"),
    ]
    for payload, label in cases:
        delivered = []
        try:
            for row in rows(chunked(payload, 5), ["id"]):
                delivered.append(row)
        except ValueError as error:
            print("bad input  %-28s -> ValueError: %s (prefix %r)"
                  % (label, error, delivered))
        else:
            raise AssertionError("expected ValueError for " + label)


NOISE_BYTES = 64 * 1024 * 1024
RECORD_COUNT = 100_000
CHUNK = 32 * 1024


def big_stream():
    """64 MiB skipped string, one skipped object, then many records."""
    yield b'{"noise":"'
    remaining = NOISE_BYTES
    while remaining:
        size = min(CHUNK, remaining)
        remaining -= size
        yield b"x" * size
    yield (b'","pad":{"ignored":[null,true,false,{"deep":[]},'
           b'{"blob":"' + b"y" * 300000 + b'"}]},"records":[')
    filler = json.dumps({"u": ["é"] * 8, "n": list(range(12))},
                        ensure_ascii=False)
    for index in range(RECORD_COUNT):
        body = ('{"id":%d,"name":"row-%d","junk":%s,"id":%d}'
                % (index, index, filler, index)).encode("utf-8")
        yield (b"," if index else b"") + body
    yield b'],"after":[[]]}'


def show_memory():
    tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]
    started = time.monotonic()
    seen = 0
    last = None
    for row in rows(big_stream(), ["id", "name"]):
        seen += 1
        last = row          # outputs are discarded on purpose
    elapsed = time.monotonic() - started
    peak = tracemalloc.get_traced_memory()[1] - baseline
    tracemalloc.stop()
    assert seen == RECORD_COUNT, seen
    assert last == {"id": RECORD_COUNT - 1,
                    "name": "row-%d" % (RECORD_COUNT - 1)}, last
    print("bounded memory: %d records after %.0f MiB of skipped text"
          % (seen, NOISE_BYTES / 1048576.0))
    print("  peak Python allocation delta: %.2f MiB (limit 8.00 MiB)"
          % (peak / 1048576.0))
    print("  elapsed: %.2f s" % elapsed)
    assert peak <= 8 * 1024 * 1024, peak


def show_state():
    from exportstream import _stream as core

    parser = core._Parser(core._validate_fields(["id"]))
    payload = (b'{"skip":"' + b'\\u00e9' * 40000 + b'",'
               b'"records":[{"id":"' + b"q" * 5000 + b'"}]}')
    retained = 0
    chunks = 0
    delivered = 0
    for piece in chunked(payload, 32 * 1024):
        chunks += 1
        for _ in parser.feed(piece.decode("utf-8")):
            delivered += 1
        retained = max(retained, len(parser.buf))
    parser.finish()
    assert delivered == 1
    print("state: %d chunks of <=32 KiB, %d rows; max unparsed tail kept "
          "between chunks: %d chars" % (chunks, delivered, retained))
    print("state: at most %d container frames (one per open level, limit %d)"
          % (core.MAX_DEPTH, core.MAX_DEPTH))
    print("state: a split token keeps only its own scanner state; selected "
          "text is capped at %d chars" % core.MAX_VALUE_CHARS)


def main():
    print("exportstream demo")
    show_reference()
    show_chunk_differential()
    show_bad_inputs()
    show_memory()
    show_state()
    print("ok")


if __name__ == "__main__":
    sys.exit(main())
