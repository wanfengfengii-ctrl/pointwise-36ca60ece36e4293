"""Malformed documents must raise ValueError and keep yielded prefixes."""

import unittest

from exportstream import rows

from .support import chunked


def collect(payload, fields=("id",), size=3):
    delivered = []
    try:
        for row in rows(chunked(payload, size), list(fields)):
            delivered.append(row)
    except ValueError as error:
        return delivered, error
    return delivered, None


class SyntaxErrorTest(unittest.TestCase):

    def assert_value_error(self, payload, fields=("id",), prefix=(),
                           exact=True):
        delivered, error = collect(payload, fields)
        self.assertIsInstance(error, ValueError, payload)
        expected = list(prefix)
        if exact:
            self.assertEqual(delivered, expected, payload)
        else:
            self.assertEqual(delivered, expected[:len(delivered)], payload)
            self.assertLessEqual(len(delivered), len(expected), payload)

    def test_truncated_document(self):
        for payload, prefix in [
                (b'{"records":[{"id":1}', ({"id": 1},)),
                (b'{"records":[{"id":1},{"id":2}', ({"id": 1}, {"id": 2})),
                (b'{"records":[{"id":', ()),
                (b'{"records":', ()),
                (b'{"records"', ()),
                (b'{"records":[', ()),
                (b'{"records":[{}', ({},)),
                (b'{', ()),
                (b'', ()),
                (b'   ', ())]:
            self.assert_value_error(payload, prefix=prefix, exact=False)

    def test_truncated_skip_region(self):
        for payload in [b'{"skip":[1,2',
                        b'{"skip":{"a":"x',
                        b'{"records":[{"id":1}],"skip":{"a":[{"b":null',
                        b'{"records":[{"id":1}],"skip":"oops']:
            prefix = ({"id": 1},) if b'{"id":1}' in payload else ()
            self.assert_value_error(payload, prefix=prefix)

    def test_trailing_comma_everywhere(self):
        for payload, prefix in [
                (b'{"records":[{"id":1},]}', ({"id": 1},)),
                (b'{"records":[{"id":1,}]}', ()),
                (b'{"records":[,{"id":1}]}', ()),
                (b'{"skip":[1,],"records":[]}', ()),
                (b'{"skip":{"a":1,},"records":[]}', ()),
                (b'{"records":[{"id":1}],}', ({"id": 1},)),
                (b'{"records":[{"id":1}],"z":{,}}', ({"id": 1},))]:
            self.assert_value_error(payload, prefix=prefix, exact=False)

    def test_missing_separators(self):
        for payload, prefix in [
                (b'{"records":[{"id":1}{"id":2}]}', ({"id": 1},)),
                (b'{"records":[{"id":1} {"id":2}]}', ({"id": 1},)),
                (b'{"records" [{"id":1}]}', ()),
                (b'{"records":[{"id" 1}]}', ()),
                (b'{"records":[{"id":1}]"x":2}', ({"id": 1},)),
                (b'{"a":1 "records":[]}', ())]:
            self.assert_value_error(payload, prefix=prefix, exact=False)

    def test_bad_literals_and_numbers(self):
        for value in (b'tru', b'fals', b'nul', b'TRUE', b'NaN',
                      b'Infinity', b'-Infinity', b'01', b'-01', b'+1',
                      b'1.', b'.5', b'1e', b'1e+', b'--1', b'1.2.3',
                      b'0x10'):
            self.assert_value_error(b'{"skip":' + value + b',"records":[]}')

    def test_bad_escapes_and_control_characters(self):
        for payload in [b'{"records":[{"id":"\\q"}]}',
                        b'{"records":[{"id":"\\u12"}]}',
                        b'{"records":[{"id":"\\u12g4"}]}',
                        b'{"records":[{"id":"a\tb"}]}',
                        b'{"records":[{"id":"a\x01b"}]}',
                        b'{"skip":"\\q","records":[]}',
                        b'{"skip":"a\x01b","records":[]}',
                        b'{"re\\qords":[]}']:
            self.assert_value_error(payload)

    def test_unquoted_keys_and_single_quotes(self):
        for payload in [b"{records:[]}", b'{"records":[]}',  # placeholder
                        b'{"records":[{id:1}]}',
                        b"{'records':[]}",
                        b'{"records":[{"id":"a"b"}]}']:
            if payload == b'{"records":[]}':
                continue
            self.assert_value_error(payload)

    def test_control_character_outside_string(self):
        self.assert_value_error(b'{"records":[\x01{"id":1}]}')

    def test_second_document(self):
        for tail in (b'{}', b'[]', b'1', b'"x"', b'null'):
            delivered, error = collect(
                b'{"records":[{"id":1}]}' + tail)
            self.assertIsInstance(error, ValueError)
            self.assertEqual(delivered, [{"id": 1}])

    def test_structure_errors(self):
        for payload, prefix in [
            (b'[]', ()),
            (b'"records"', ()),
            (b'1', ()),
            (b'{}', ()),
            (b'{"a":1}', ()),
            (b'{"records":{}}', ()),
            (b'{"records":[{"id":1}], "records":[]}', ({"id": 1},)),
            (b'{"records":[{"id":1}],"r\\u0065cords":[]}', ({"id": 1},)),
            (b'{"records":1}', ()),
            (b'{"records":null}', ()),
            (b'{"records":"x"}', ()),
            (b'{"records":[[{"id":1}]]}', ()),
            (b'{"records":[1,{"id":1}]}', ()),
            (b'{"records":[null]}', ()),
            (b'{"records":[[1]]}', ()),
            (b'{"records":[{"id":1}],"records":[{"id":2}]}', ({"id": 1},)),
        ]:
            self.assert_value_error(payload, prefix=prefix)

    def test_selected_value_type_errors(self):
        for value in (b'1.5', b'-1.5', b'0.0', b'1e5', b'1E+2', b'[]',
                      b'[1]', b'{}', b'{"a":1}', b'--1', b'1e1000'):
            delivered, error = collect(
                b'{"records":[{"id":' + value + b'}]}')
            self.assertIsInstance(error, ValueError, value)
            self.assertEqual(delivered, [], value)

    def test_selected_integer_range(self):
        for value, ok in [(b'9223372036854775807', True),
                          (b'-9223372036854775808', True),
                          (b'9223372036854775808', False),
                          (b'-9223372036854775809', False),
                          (b'99999999999999999999999', False),
                          (b'-99999999999999999999999', False)]:
            payload = b'{"records":[{"id":' + value + b'}]}'
            if ok:
                self.assertEqual(collect(payload)[0], [{"id": int(value)}])
            else:
                self.assert_value_error(payload)

    def test_error_after_delivered_prefix(self):
        payload = (b'{"records":[{"id":1},{"id":2},{"id":3},'
                   b'{"id":4,"skip":{"x":[1,]}}]}')
        delivered, error = collect(payload, size=2)
        self.assertEqual(delivered, [{"id": 1}, {"id": 2}, {"id": 3},
                                     {"id": 4}][:len(delivered)])
        self.assertIsInstance(error, ValueError)
        self.assertGreater(len(delivered), 0)
        self.assertLessEqual(len(delivered), 4)

    def test_depth_limit(self):
        from exportstream import MAX_DEPTH

        # root object is level 1, the "records" array level 2, a record 3
        def make(nested):
            return (b'{"records":[{"id":1}],"skip":' +
                    b'[' * nested + b'0' + b']' * nested + b'}')

        # levels used by the skipped value: root(1) + nested arrays
        allowed = MAX_DEPTH - 1  # root object counts as level 1
        self.assertEqual(collect(make(allowed))[0], [{"id": 1}])
        delivered, error = collect(make(allowed + 1))
        self.assertIsInstance(error, ValueError)
        self.assertEqual(delivered, [{"id": 1}])

    def test_depth_limit_in_records(self):
        from exportstream import MAX_DEPTH

        def make(nested):
            return (b'{"records":[' +
                    b'{"id":1,"skip":' + b'[' * nested + b'0' +
                    b']' * nested + b'}]}')

        # root(1) + records array(2) + record(3) + nested
        allowed = MAX_DEPTH - 3
        self.assertEqual(collect(make(allowed))[0], [{"id": 1}])
        delivered, error = collect(make(allowed + 1))
        self.assertIsInstance(error, ValueError)
        self.assertEqual(delivered, [])

    def test_utf8_errors(self):
        for bad in (b'\xff', b'\xfe', b'\xc0\xaf', b'\xed\xa0\x80',
                    b'\xf0\x9f\x98', b'\xe4\xb8'):
            payload = b'{"records":[{"id":"' + bad + b'"}]}'
            delivered, error = collect(payload, size=1)
            self.assertIsInstance(error, ValueError)
            self.assertEqual(delivered, [], bad)

    def test_utf8_error_in_skip_region(self):
        delivered, error = collect(
            b'{"skip":"\xff","records":[{"id":1}]}', size=2)
        self.assertIsInstance(error, ValueError)
        self.assertEqual(delivered, [])

    def test_truncated_utf8_at_end(self):
        for bad in (b'\xc3', b'\xe4\xb8', b'\xf0\x9f\x98'):
            delivered, error = collect(b'{"skip":"x' + bad, size=2)
            self.assertIsInstance(error, ValueError)

    def test_bom_is_not_accepted(self):
        delivered, error = collect(b'\xef\xbb\xbf{"records":[]}')
        self.assertIsInstance(error, ValueError)
