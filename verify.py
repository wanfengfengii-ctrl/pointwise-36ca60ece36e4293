"""Machine-checkable verification used by .pointwise-runtime.json.

Runs the full unit-test suite, then re-checks the reference example exactly as
the task states it and enforces the bounded-memory claim over a real 64 MiB
stream.  Exit status 0 means every check passed.
"""

import sys
import tracemalloc

from exportstream import rows

REFERENCE = (b'{"records":[{"id":1},{"id":null},{}]}', ["id"],
             [{"id": 1}, {"id": None}, {}])


def check_reference():
    projected = list(rows(iter([REFERENCE[0]]), REFERENCE[1]))
    assert projected == REFERENCE[2], projected
    print("reference example: %r" % (projected,))


def check_bad_document():
    delivered = []
    with_errors = 0
    try:
        for row in rows(iter([b'{"records":[{"id":1}],"skip":[1,]}']), ["id"]):
            delivered.append(row)
    except ValueError:
        with_errors = 1
    assert with_errors and delivered == [{"id": 1}], delivered
    print("bad document: ValueError raised, prefix kept: %r" % (delivered,))


def check_memory():
    def stream():
        yield b'{"noise":"'
        remaining = 64 * 1024 * 1024
        block = b"x" * (32 * 1024)
        while remaining:
            piece = block if remaining >= len(block) else block[:remaining]
            remaining -= len(piece)
            yield piece
        yield b'","records":['
        for index in range(50_000):
            yield ((b"," if index else b"") +
                   b'{"id":%d,"name":"row-%d"}' % (index, index))
        yield b"]}"

    tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]
    seen = 0
    for _ in rows(stream(), ["id", "name"]):
        seen += 1
    peak = tracemalloc.get_traced_memory()[1] - baseline
    tracemalloc.stop()
    assert seen == 50_000, seen
    assert peak <= 8 * 1024 * 1024, peak
    print("bounded memory: 50k records + 64 MiB skipped, "
          "peak allocation %.2f MiB" % (peak / 1048576.0))


def check_tests():
    import unittest

    loader = unittest.TestLoader()
    suite = loader.discover("tests", top_level_dir=".")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        return 1
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--no-tests":
        skip_tests = True
    else:
        skip_tests = False
    failures = check_tests() if not skip_tests else 0
    check_reference()
    check_bad_document()
    check_memory()
    print("verify: %s" % ("ok" if not failures else "FAILED"))
    return failures


if __name__ == "__main__":
    sys.exit(main())
