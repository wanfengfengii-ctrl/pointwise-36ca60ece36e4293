"""Every chunk boundary must produce the same result as one whole chunk."""

import json
import unittest

from exportstream import rows

from .support import chunked, reference


DOCUMENT = (
    '{"meta":{"tags":["a","b",{"deep":[null,true]}],"n":1.5,"s":"é中😀"},'
    '"records":['
    '{"id":7,"text":"line\\nbreak é 😀","name":"x","neg":-12},'
    '{"id":-12,"name":null,"extra":{"big":[1,2,3]}},'
    '{"name":"y","id":0},'
    '{"id":"\\ud83d\\ude00\\ud800"},'
    '{}'
    '],"tail":[[[]]]}'
).encode("utf-8")

FIELDS = ["id", "name", "text", "neg"]


class ChunkBoundaryTest(unittest.TestCase):

    def test_every_split_point(self):
        expected = list(rows(iter([DOCUMENT]), FIELDS))
        self.assertEqual(expected, reference(DOCUMENT, FIELDS))
        for split in range(len(DOCUMENT) + 1):
            got = list(rows(iter([DOCUMENT[:split], DOCUMENT[split:]],
                                ), FIELDS))
            self.assertEqual(got, expected, "split at byte %d" % split)

    def test_every_chunk_size(self):
        expected = list(rows(iter([DOCUMENT]), FIELDS))
        for size in range(1, 97):
            self.assertEqual(list(rows(chunked(DOCUMENT, size), FIELDS)),
                             expected, "chunk size %d" % size)

    def test_one_byte_chunks(self):
        expected = list(rows(iter([DOCUMENT]), FIELDS))
        self.assertEqual(list(rows(chunked(DOCUMENT, 1), FIELDS)), expected)

    def test_every_prefix_truncates_with_value_error(self):
        full = list(rows(iter([DOCUMENT]), FIELDS))
        for cut in range(len(DOCUMENT)):
            delivered = []
            with self.assertRaises(ValueError, msg="cut at %d" % cut):
                for row in rows(chunked(DOCUMENT[:cut], 3), FIELDS):
                    delivered.append(row)
            self.assertEqual(delivered, full[:len(delivered)])

    def test_extra_whitespace_across_boundaries(self):
        payload = b'  \n\t{"records"  :  [ {"id"  : 1} , { "id" : 2 } ]  } \r'
        self.assertEqual(list(rows(chunked(payload, 1), ["id"]),
                              ), [{"id": 1}, {"id": 2}])

    def test_multibyte_split_inside_selected_string(self):
        text = "é中😀"
        payload = ('{"records":[{"id":%s}]}' % json.dumps(text)).encode()
        for size in range(1, 12):
            self.assertEqual(list(rows(chunked(payload, size), ["id"]),
                                  ), [{"id": text}])

    def test_long_tokens_split_at_every_offset(self):
        long_text = "q" * 300
        long_number = 9223372036854775807
        payload = ('{"records":[{"id":%d,"text":"%s"},{"id":"%s"}]}'
                   % (long_number, long_text, long_text)).encode()
        expected = [{"id": long_number, "text": long_text},
                    {"id": long_text}]
        for size in (1, 2, 5, 8, 32, 33, 512):
            self.assertEqual(
                list(rows(chunked(payload, size), ["id", "text"]),
                     ), expected)

    def test_escaped_sequence_split_at_every_offset(self):
        payload = (b'{"records":[{"v":"\\ud83d\\ude00\\u00e9\\n\\"x\\\\y"}]}')
        expected = [{"v": "\U0001f600\xe9\n\"x\\y"}]
        for size in range(1, 40):
            self.assertEqual(list(rows(chunked(payload, size), ["v"]),
                                  ), expected)

    def test_literal_and_number_split_at_every_offset(self):
        payload = (b'{"records":[{"v":true},{"v":false},{"v":null},'
                   b'{"v":-1234567890123}]}')
        expected = [{"v": True}, {"v": False}, {"v": None},
                    {"v": -1234567890123}]
        for size in range(1, 28):
            self.assertEqual(list(rows(chunked(payload, size), ["v"]),
                                  ), expected)

    def test_member_name_split_at_every_offset(self):
        payload = b'{"records":[{"identifier":1},{"id":2}]}'
        expected = [{"identifier": 1}, {"id": 2}]
        for size in range(1, 30):
            self.assertEqual(
                list(rows(chunked(payload, size), ["identifier", "id"]),
                     ), expected)

    def test_bytes_like_inputs(self):
        payload = b'{"records":[{"id":1}]}'
        self.assertEqual(list(rows([memoryview(payload)], ["id"]),),
                         [{"id": 1}])
        self.assertEqual(list(rows([bytearray(payload)], ["id"]),),
                         [{"id": 1}])
        with self.assertRaises(TypeError):
            list(rows(["text"], ["id"]))
