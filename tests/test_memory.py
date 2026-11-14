"""Memory must not grow with record count or with skipped payload size."""

import tracemalloc
import unittest

from exportstream import rows

from .support import chunked

LIMIT = 8 * 1024 * 1024


def run(factory, fields):
    """Consume a stream, discard every row, return peak traced bytes."""
    tracemalloc.start()
    tracemalloc.clear_traces()
    baseline = tracemalloc.get_traced_memory()[0]
    rows_seen = 0
    for _ in rows(factory(), fields):
        rows_seen += 1
    peak = tracemalloc.get_traced_memory()[1] - baseline
    tracemalloc.stop()
    return peak, rows_seen


def noisy_stream(noise_chars, record_count, record_size=200):
    """A stream whose skipped region is ``noise_chars`` bytes long."""
    yield b'{"noise":"'
    remaining = noise_chars
    block = b"z" * (32 * 1024)
    while remaining:
        piece = block if remaining >= len(block) else block[:remaining]
        remaining -= len(piece)
        yield piece
    yield b'","records":['
    filler = b"q" * record_size
    for index in range(record_count):
        yield ((b"," if index else b"") +
               b'{"id":%d,"junk":{"deep":["%s"]}}' % (index, filler))
    yield b"]}"


class MemoryTest(unittest.TestCase):

    def test_skipped_payload_does_not_grow_memory(self):
        small, count = run(lambda: noisy_stream(64 * 1024, 500), ["id"])
        self.assertEqual(count, 500)
        large, count = run(lambda: noisy_stream(64 * 1024 * 1024, 500), ["id"])
        self.assertEqual(count, 500)
        self.assertLessEqual(large, LIMIT, (small, large))
        self.assertLess(large - small, 1024 * 1024, (small, large))

    def test_record_count_does_not_grow_memory(self):
        few, _ = run(lambda: noisy_stream(0, 100), ["id"])
        many, count = run(lambda: noisy_stream(0, 100_000), ["id"])
        self.assertEqual(count, 100_000)
        self.assertLessEqual(many, LIMIT, (few, many))
        self.assertLess(many - few, 1024 * 1024, (few, many))

    def test_combined_64_mib_and_many_records(self):
        peak, count = run(lambda: noisy_stream(64 * 1024 * 1024, 100_000),
                          ["id"])
        self.assertEqual(count, 100_000)
        self.assertLessEqual(peak, LIMIT, peak)

    def test_selected_strings_are_bounded_by_record(self):
        def stream(length):
            yield b'{"records":['
            for index in range(200):
                yield ((b"," if index else b"") +
                       ('{"id":%d,"name":"%s"}' %
                        (index, "n" * length)).encode())
            yield b"]}"

        short, _ = run(lambda: stream(16), ["id", "name"])
        long, _ = run(lambda: stream(3000), ["id", "name"])
        self.assertLessEqual(long, LIMIT)
        self.assertLess(long - short, 3 * 1024 * 1024)

    def test_bounded_for_any_chunk_size(self):
        payload = b"".join(noisy_stream(256 * 1024, 20))
        for size in (1, 16, 4096, 32 * 1024):
            peak, count = run(
                lambda: chunked(payload, size), ["id"])
            self.assertEqual(count, 20)
            self.assertLessEqual(peak, LIMIT, size)
