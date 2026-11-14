import json
import unittest

from exportstream import rows

from .support import chunked, reference


class ProjectionTest(unittest.TestCase):

    def collect(self, payload, fields, size=5):
        return list(rows(chunked(payload, size), fields))

    def test_documented_example(self):
        payload = b'{"records":[{"id":1},{"id":null},{}]}'
        self.assertEqual(self.collect(payload, ["id"], 1),
                         [{"id": 1}, {"id": None}, {}])

    def test_empty_records_array_yields_nothing(self):
        self.assertEqual(self.collect(b'{"records":[]}', ["id"]), [])
        self.assertEqual(
            self.collect(b'{"a":[1,2,{"b":null}],"records":[]}', ["id"]), [])

    def test_missing_vs_null_and_field_order(self):
        payload = (b'{"records":[{"b":2,"a":1,"c":null},'
                   b'{"a":null},{"b":"x"},{}]}')
        self.assertEqual(
            self.collect(payload, ["a", "b", "c"], 3),
            [{"a": 1, "b": 2, "c": None},
             {"a": None},
             {"b": "x"},
             {}])
        # insertion order follows the fields list, not the document
        self.assertEqual(list(self.collect(payload, ["c", "a", "b"], 2)[0]),
                         ["c", "a", "b"])

    def test_repeated_field_keeps_last_value(self):
        payload = (b'{"records":[{"id":1,"id":null,"id":"z"},'
                   b'{"id":1,"other":2,"id":3}]}')
        self.assertEqual(self.collect(payload, ["id"], 3),
                         [{"id": "z"}, {"id": 3}])

    def test_scalar_kinds(self):
        payload = (b'{"records":[{"v":"s"},{"v":-9223372036854775808},'
                   b'{"v":9223372036854775807},{"v":true},{"v":false},'
                   b'{"v":null},{"v":"\\u0000"},{"v":"\\ud83d\\ude00"}]}')
        rows_out = self.collect(payload, ["v"], 4)
        self.assertEqual(rows_out, [
            {"v": "s"},
            {"v": -9223372036854775808},
            {"v": 9223372036854775807},
            {"v": True},
            {"v": False},
            {"v": None},
            {"v": "\x00"},
            {"v": "\U0001f600"},
        ])

    def test_lone_surrogate_is_preserved_like_json(self):
        payload = b'{"records":[{"v":"\\ud800"},{"v":"a\\ud800\\udc00b"}]}'
        got = self.collect(payload, ["v"], 2)
        self.assertEqual(got, [{"v": "\ud800"},
                               {"v": "a\U00010000b"}])

    def test_selected_string_with_escapes(self):
        payload = b'{"records":[{"v":"a\\"b\\\\c\\/d\\b\\f\\n\\r\\t\\u00e9"}]}'
        self.assertEqual(self.collect(payload, ["v"], 1),
                         [{"v": "a\"b\\c/d\b\f\n\r\t\xe9"}])

    def test_unselected_members_may_be_anything(self):
        payload = json.dumps({
            "records": [
                {"id": 1, "x": {"deep": [[[{"k": None}]]], "n": 1.5},
                 "id": 2},
                {"id": 3, "y": [True, False, None, 1e300, ""]},
            ],
            "meta": {"z": ["a", "b", {"c": [None]}]},
        }, ensure_ascii=False).encode()
        self.assertEqual(self.collect(payload, ["id"], 7),
                         [{"id": 2}, {"id": 3}])

    def test_keys_with_escapes(self):
        payload = b'{"records":[{"i\\u0064":7}]}'
        self.assertEqual(self.collect(payload, ["id"], 2), [{"id": 7}])

    def test_records_member_name_with_escape(self):
        payload = b'{"rec\\u006frds":[{"id":1}]}'
        self.assertEqual(self.collect(payload, ["id"], 1), [{"id": 1}])

    def test_records_before_other_members(self):
        payload = (b'{"records":[{"id":1}],'
                   b'"history":[{"id":2,"id":3},"skip",{"nested":{}}],'
                   b'"summary":{"count":2}}')
        self.assertEqual(self.collect(payload, ["id"], 1), [{"id": 1}])

    def test_whitespace_and_chunk_sizes(self):
        payload = (b' \n\t { "records" : [ { "id" : 1 } , { "id" : 2 } ] } '
                   b'\r\n\t ')
        for size in (1, 2, 3, 4, 5, 8, 13, 64, 4096):
            self.assertEqual(list(rows(chunked(payload, size), ["id"]),
                                 ), [{"id": 1}, {"id": 2}])

    def test_matches_json_loads_random_documents(self):
        import random
        rng = random.Random(2024)
        for trial in range(40):
            records = []
            for _ in range(rng.randint(0, 4)):
                record = {}
                for _ in range(rng.randint(0, 4)):
                    record[rng.choice(["id", "a", "b", "c"])] = (
                        rng.choice([0, 1, -1, 2 ** 40, "s", "", None,
                                    True, False]))
                records.append(record)
            payload = json.dumps({"records": records,
                                  "skip": {"x": [None, 1, "é"]}},
                                 ensure_ascii=False).encode()
            fields = rng.choice([["id"], ["a", "b"], ["a", "b", "c"]])
            expected = reference(payload, fields)
            for size in (1, 3, 7, 64):
                self.assertEqual(list(rows(chunked(payload, size), fields)),
                                 expected, (payload, fields, size))

    def test_fields_argument_validation(self):
        with self.assertRaises(ValueError):
            rows(iter([b""]), [])
        with self.assertRaises(ValueError):
            rows(iter([b""]), ["id"] * 2)
        with self.assertRaises(ValueError):
            rows(iter([b""]), ["f%d" % i for i in range(17)])
        with self.assertRaises(TypeError):
            rows(iter([b""]), "id")
        with self.assertRaises(TypeError):
            rows(iter([b""]), [1])
        with self.assertRaises(TypeError):
            rows(iter([b""]), 42)
        self.assertEqual(
            list(rows(iter([b'{"records":[{"a":1},{}]}']), ["a", "b"])),
            [{"a": 1}, {}])
