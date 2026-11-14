"""Chunked, projection-only JSON record reader.

The parser is one incremental state machine.  It consumes a chunk at a time and
never keeps what it has already validated: values outside the selected
projection are checked for syntax and dropped immediately.  Tokens that a
chunk boundary may split (strings, numbers, literals, member names) are
handled by small scanners that carry bounded state; the sliding text buffer
holds at most one chunk plus a single split token.  Retained memory therefore
does not grow with the number of records or with the length of skipped
regions.
"""

import codecs
import re

__all__ = ["rows"]

#: RFC 8259 section 9 nesting limit, counting the root value.
MAX_DEPTH = 32
#: ``fields`` holds between 1 and 16 distinct names.
MAX_FIELDS = 16
#: One projection is at most 64 KiB with ``json.dumps`` default parameters.
MAX_PROJECTION = 64 * 1024
#: Hard cap on one retained scalar.  A conforming document cannot exceed it;
#: enforcing it keeps memory bounded for documents that do not conform.
MAX_VALUE_CHARS = MAX_PROJECTION

_WS = " \t\n\r"
_STOP = re.compile(r'["\\\x00-\x1f]')
_NUMBER = re.compile(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?')
_TERMINATORS = frozenset(" \t\n\r,]}")
_HEX = frozenset("0123456789abcdefABCDEF")
_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
_LITERALS = {
    "t": ("true", True),
    "f": ("false", False),
    "n": ("null", None),
}

# Container kinds.
_TOP = 0        # the top-level object
_RECORDS = 1    # the array that is the value of the "records" member
_RECORD = 2     # an object inside that array
_SKIP_OBJ = 3   # any other object
_SKIP_ARR = 4   # any other array
_OBJECT_KINDS = frozenset((_TOP, _RECORD, _SKIP_OBJ))

# Frame states (objects and arrays share the names).
_S_KEY = 0      # object: a member name or '}'
_S_COLON = 1    # object: ':'
_S_VALUE = 2    # a value; for an array, an element or ']'
_S_COMMA = 3    # ',' or the closing bracket

# Scanner roles.
_R_NAME_TOP = 0
_R_NAME_RECORD = 1
_R_NAME_SKIP = 2
_R_VALUE_SKIP = 3
_R_VALUE_SELECTED = 4

# Number scanner states.
_N_START = 0
_N_MINUS = 1
_N_ZERO = 2
_N_INT = 3
_N_DOT = 4
_N_FRAC = 5
_N_E = 6
_N_ESIGN = 7
_N_EXP = 8
_N_ACCEPT = frozenset((_N_ZERO, _N_INT, _N_FRAC, _N_EXP))

_DIGITS = frozenset("0123456789")
_LITERAL_TAIL = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_INT64_MIN = -0x8000000000000000
_INT64_MAX = 0x7FFFFFFFFFFFFFFF


class _Frame:
    __slots__ = ("kind", "depth", "state", "allow_close", "member_name",
                 "member_selected", "acc", "records_count")

    def __init__(self, kind, depth):
        self.kind = kind
        self.depth = depth
        self.state = _S_KEY if kind in _OBJECT_KINDS else _S_VALUE
        self.allow_close = True
        self.member_name = None
        self.member_selected = False
        self.acc = {} if kind == _RECORD else None
        self.records_count = 0


class _StrScan:
    """Incremental JSON string scanner; the opening quote is consumed."""

    __slots__ = ("role", "frame", "keep", "cap", "strict", "parts",
                 "length", "overflow", "state", "hexbuf", "high", "value",
                 "done")

    def __init__(self, role, frame, keep, cap, strict):
        self.role = role
        self.frame = frame
        self.keep = keep
        self.cap = cap
        self.strict = strict
        self.parts = [] if keep else None
        self.length = 0
        self.overflow = False
        self.state = 0   # 0 text, 1 after a backslash, 2 inside \uXXXX
        self.hexbuf = []
        self.high = None
        self.value = None
        self.done = False

    def _put(self, text):
        if not self.keep or not text:
            return
        if self.overflow:
            self.length += len(text)
            return
        total = self.length + len(text)
        if total > self.cap:
            if self.strict:
                raise ValueError(
                    "selected string exceeds the 64 KiB projection limit")
            self.overflow = True
            self.length = total
            self.parts = None
            return
        self.parts.append(text)
        self.length = total

    def _put_char(self, char):
        if self.keep and not self.overflow:
            self._put(char)

    def _flush_high(self):
        high = self.high
        if high is not None:
            self.high = None
            self._put_char(chr(high))

    def _codepoint(self, code):
        if self.high is not None:
            high = self.high
            self.high = None
            if 0xDC00 <= code <= 0xDFFF:
                self._put_char(chr(0x10000 + ((high - 0xD800) << 10)
                                  + (code - 0xDC00)))
                return
            self._put_char(chr(high))
        if 0xD800 <= code <= 0xDBFF:
            self.high = code
        else:
            self._put_char(chr(code))

    def _finish(self):
        self._flush_high()
        if self.keep and not self.overflow:
            self.value = "".join(self.parts)
        self.parts = None
        self.done = True

    def feed(self, buf, pos, n):
        while pos < n:
            state = self.state
            if state == 0:
                match = _STOP.search(buf, pos, n)
                if match is None:
                    if self.keep:
                        self._flush_high()
                        self._put(buf[pos:n])
                    return n, False
                stop = match.start()
                if stop > pos:
                    if self.keep:
                        self._flush_high()
                        self._put(buf[pos:stop])
                char = buf[stop]
                pos = stop + 1
                if char == '"':
                    self._finish()
                    return pos, True
                if char == "\\":
                    # A following \uDC00 may pair with a pending high
                    # surrogate, so it is flushed only when that is ruled out.
                    self.state = 1
                    continue
                raise ValueError("invalid control character in a string")
            if state == 1:
                char = buf[pos]
                pos += 1
                if char == "u":
                    self.hexbuf = []
                    self.state = 2
                else:
                    decoded = _ESCAPES.get(char)
                    if decoded is None:
                        raise ValueError("invalid escape sequence")
                    if self.keep:
                        self._flush_high()
                        self._put_char(decoded)
                    self.state = 0
                continue
            char = buf[pos]
            pos += 1
            if char not in _HEX:
                raise ValueError("invalid \\u escape")
            self.hexbuf.append(char)
            if len(self.hexbuf) == 4:
                code = int("".join(self.hexbuf), 16)
                self.hexbuf = []
                self.state = 0
                self._codepoint(code)
        return pos, False


class _NumScan:
    """Incremental JSON number scanner."""

    __slots__ = ("role", "frame", "keep", "chars", "length", "fractional",
                 "state", "value", "done")

    def __init__(self, role, frame, keep):
        self.role = role
        self.frame = frame
        self.keep = keep
        self.chars = [] if keep else None
        self.length = 0
        self.fractional = False
        self.state = _N_START
        self.value = None
        self.done = False

    def _complete(self):
        if self.state not in _N_ACCEPT:
            raise ValueError("invalid number")
        if not self.keep:
            self.done = True
            return
        if self.fractional:
            raise ValueError(
                "selected value must be a string, signed 64-bit integer, "
                "boolean or null")
        value = int("".join(self.chars))
        if value < _INT64_MIN or value > _INT64_MAX:
            raise ValueError(
                "selected integer is outside the signed 64-bit range")
        self.value = value
        self.done = True

    def feed(self, buf, pos, n):
        state = self.state
        digits = _DIGITS
        start = pos
        while pos < n:
            char = buf[pos]
            if state == _N_INT or state == _N_FRAC or state == _N_EXP:
                if char in digits:
                    pos += 1
                    continue
                if state == _N_INT and char == ".":
                    state = _N_DOT
                    self.fractional = True
                elif state != _N_EXP and (char == "e" or char == "E"):
                    state = _N_E
                    self.fractional = True
                else:
                    break
                self._take(buf, start, pos + 1)
                pos += 1
                start = pos
                continue
            if state == _N_ZERO:
                if char == ".":
                    state = _N_DOT
                    self.fractional = True
                elif char == "e" or char == "E":
                    state = _N_E
                    self.fractional = True
                elif char in digits:
                    raise ValueError("invalid number: leading zero")
                else:
                    break
            elif state == _N_START:
                if char == "-":
                    state = _N_MINUS
                elif char == "0":
                    state = _N_ZERO
                elif char in digits:
                    state = _N_INT
                else:
                    raise ValueError("invalid number")
            elif state == _N_MINUS:
                if char == "0":
                    state = _N_ZERO
                elif char in digits:
                    state = _N_INT
                else:
                    raise ValueError("invalid number")
            elif state == _N_DOT:
                if char in digits:
                    state = _N_FRAC
                else:
                    raise ValueError("invalid number: expected a digit")
            elif state == _N_E:
                if char == "+" or char == "-":
                    state = _N_ESIGN
                elif char in digits:
                    state = _N_EXP
                else:
                    raise ValueError("invalid number: expected an exponent")
            else:  # _N_ESIGN
                if char in digits:
                    state = _N_EXP
                else:
                    raise ValueError("invalid number: expected an exponent")
            self._take(buf, start, pos + 1)
            pos += 1
            start = pos
        self._take(buf, start, pos)
        self.state = state
        if pos >= n:
            return pos, False
        self._complete()
        return pos, True

    def _take(self, buf, start, pos):
        if not self.keep or pos == start:
            return
        chunk = buf[start:pos]
        if self.length + len(chunk) > 20:
            raise ValueError(
                "selected integer is outside the signed 64-bit range")
        self.chars.append(chunk)
        self.length += len(chunk)


class _LitScan:
    """Incremental true/false/null scanner."""

    __slots__ = ("role", "frame", "literal", "value", "index", "done")

    def __init__(self, role, frame, literal, value):
        self.role = role
        self.frame = frame
        self.literal = literal
        self.value = value
        self.index = 0
        self.done = False

    def feed(self, buf, pos, n):
        literal = self.literal
        index = self.index
        size = len(literal)
        while pos < n and index < size:
            if buf[pos] != literal[index]:
                raise ValueError("invalid literal")
            index += 1
            pos += 1
        self.index = index
        if index == size:
            self.done = True
            return pos, True
        return pos, False


class _Parser:
    __slots__ = ("fields", "field_set", "max_name", "frames", "buf",
                 "pending", "root_done")

    def __init__(self, fields):
        self.fields = fields
        self.field_set = frozenset(fields)
        self.max_name = max((len(name) for name in fields), default=0)
        self.frames = []
        self.buf = ""
        self.pending = None
        self.root_done = False

    # -- driver ----------------------------------------------------------
    def feed(self, text):
        if text:
            self.buf = self.buf + text
        buf = self.buf
        n = len(buf)
        pos = 0
        while True:
            scan = self.pending
            if scan is not None:
                pos, done = scan.feed(buf, pos, n)
                if not done:
                    break
                self.pending = None
                result = self._apply(scan)
                if result is not None:
                    yield result
                continue
            pos = _skip_ws(buf, pos, n)
            if pos >= n:
                break
            frames = self.frames
            if not frames:
                if self.root_done:
                    raise ValueError("extra data after the top-level object")
                if buf[pos] != "{":
                    raise ValueError("the top-level value must be an object")
                frames.append(_Frame(_TOP, 1))
                pos += 1
                continue
            frame = frames[-1]
            char = buf[pos]
            if frame.kind in _OBJECT_KINDS:
                if frame.state == _S_KEY:
                    if char == "}":
                        if not frame.allow_close:
                            raise ValueError("trailing comma in an object")
                        pos += 1
                        result = self._close()
                        if result is not None:
                            yield result
                        continue
                    if char != '"':
                        raise ValueError("expected a member name or '}'")
                    pos, result = self._member_name(frame, pos, n)
                    if result is not None:
                        yield result
                    continue
                if frame.state == _S_COLON:
                    if char != ":":
                        raise ValueError("expected ':' after a member name")
                    frame.state = _S_VALUE
                    pos += 1
                    continue
                if frame.state == _S_VALUE:
                    pos = self._begin_value(frame, pos)
                    continue
                if char == ",":
                    frame.state = _S_KEY
                    frame.allow_close = False
                    pos += 1
                    continue
                if char == "}":
                    pos += 1
                    result = self._close()
                    if result is not None:
                        yield result
                    continue
                raise ValueError("expected ',' or '}'")
            # array frames
            if frame.state == _S_VALUE:
                if char == "]":
                    if not frame.allow_close:
                        raise ValueError("trailing comma in an array")
                    pos += 1
                    result = self._close()
                    if result is not None:
                        yield result
                    continue
                pos = self._begin_value(frame, pos)
                continue
            if char == ",":
                frame.state = _S_VALUE
                frame.allow_close = False
                pos += 1
                continue
            if char == "]":
                pos += 1
                result = self._close()
                if result is not None:
                    yield result
                continue
            raise ValueError("expected ',' or ']'")
        self.buf = buf[pos:]

    # -- member names ----------------------------------------------------
    def _member_name(self, frame, pos, n):
        """Parse a member name starting at the opening quote."""
        end = _plain_end(self.buf, pos + 1, n)
        if end >= 0:
            raw = self.buf[pos + 1:end]
            if frame.kind == _TOP:
                name = raw if len(raw) <= 7 else None
                if name == "records":
                    if frame.records_count:
                        raise ValueError(
                            "'records' appears more than once in the "
                            "top-level object")
                    frame.records_count = 1
                frame.member_name = name
            elif frame.kind == _RECORD:
                frame.member_name = raw
                frame.member_selected = raw in self.field_set
            else:
                frame.member_name = None
            frame.state = _S_COLON
            return end + 1, None
        keep = frame.kind != _SKIP_OBJ
        cap = 7 if frame.kind == _TOP else self.max_name
        self.pending = _StrScan(
            _R_NAME_TOP if frame.kind == _TOP else
            _R_NAME_RECORD if frame.kind == _RECORD else _R_NAME_SKIP,
            frame, keep, cap, False)
        return pos + 1, None

    # -- values ----------------------------------------------------------
    def _begin_value(self, frame, pos):
        buf = self.buf
        char = buf[pos]
        kind = frame.kind
        if kind == _RECORDS:
            if char != "{":
                raise ValueError("'records' must be an array of objects")
            self._push(_RECORD)
            return pos + 1
        if kind == _TOP and frame.member_name == "records":
            if char != "[":
                raise ValueError("'records' must be an array of objects")
            self._push(_RECORDS)
            return pos + 1
        if frame.member_selected:
            if char == "{" or char == "[":
                raise ValueError(
                    "selected value must be a string, signed 64-bit integer, "
                    "boolean or null")
            return self._begin_scalar(frame, pos, True)
        if char == "{":
            self._push(_SKIP_OBJ)
            return pos + 1
        if char == "[":
            self._push(_SKIP_ARR)
            return pos + 1
        return self._begin_scalar(frame, pos, False)

    def _begin_scalar(self, frame, pos, selected):
        char = self.buf[pos]
        role = _R_VALUE_SELECTED if selected else _R_VALUE_SKIP
        if char == '"':
            end = _plain_end(self.buf, pos + 1, len(self.buf))
            if end >= 0:
                raw = self.buf[pos + 1:end]
                if selected:
                    if len(raw) > MAX_VALUE_CHARS:
                        raise ValueError(
                            "selected string exceeds the 64 KiB projection "
                            "limit")
                    frame.acc[frame.member_name] = raw
                frame.member_name = None
                frame.member_selected = False
                frame.state = _S_COMMA
                return end + 1
            if not selected:
                self.pending = _StrScan(role, frame, False, 0, False)
            else:
                self.pending = _StrScan(
                    role, frame, True, MAX_VALUE_CHARS, True)
            return pos + 1
        if char == "-" or "0" <= char <= "9":
            match = _NUMBER.match(self.buf, pos)
            if match is not None:
                end = match.end()
                size = len(self.buf)
                if end < size and self.buf[end] in _TERMINATORS:
                    # A delimiter follows, so the match is the whole token.
                    return self._finish_number(frame, selected,
                                               match.group(), end)
            self.pending = _NumScan(role, frame, selected)
            return pos
        literal = _LITERALS.get(char)
        if literal is not None:
            text, value = literal
            end = pos + len(text)
            if end < len(self.buf) and self.buf.startswith(text, pos):
                following = self.buf[end]
                if following not in _LITERAL_TAIL:
                    return self._finish_literal(frame, selected, value, end)
            self.pending = _LitScan(role, frame, text, value)
            return pos
        raise ValueError("expected a JSON value")

    def _finish_number(self, frame, selected, text, end):
        """A number that is complete inside the current buffer."""
        if selected:
            if "." in text or "e" in text or "E" in text:
                raise ValueError(
                    "selected value must be a string, signed 64-bit integer, "
                    "boolean or null")
            if len(text) > 20:
                raise ValueError(
                    "selected integer is outside the signed 64-bit range")
            value = int(text)
            if value < _INT64_MIN or value > _INT64_MAX:
                raise ValueError(
                    "selected integer is outside the signed 64-bit range")
            frame.acc[frame.member_name] = value
        frame.member_name = None
        frame.member_selected = False
        frame.state = _S_COMMA
        return end

    def _finish_literal(self, frame, selected, value, end):
        if selected:
            frame.acc[frame.member_name] = value
        frame.member_name = None
        frame.member_selected = False
        frame.state = _S_COMMA
        return end

    def _push(self, kind):
        parent = self.frames[-1]
        frame = _Frame(kind, parent.depth + 1)
        if frame.depth > MAX_DEPTH:
            raise ValueError(
                "nesting exceeds the RFC 8259 limit of %d levels"
                % MAX_DEPTH)
        self.frames.append(frame)

    # -- completion ------------------------------------------------------
    def _apply(self, scan):
        frame = scan.frame
        role = scan.role
        if role == _R_NAME_TOP:
            name = scan.value
            if name == "records":
                if frame.records_count:
                    raise ValueError(
                        "'records' appears more than once in the top-level "
                        "object")
                frame.records_count = 1
            frame.member_name = name
            frame.state = _S_COLON
            return None
        if role == _R_NAME_RECORD:
            value = scan.value
            frame.member_name = value
            frame.member_selected = (value is not None
                                     and value in self.field_set)
            frame.state = _S_COLON
            return None
        if role == _R_NAME_SKIP:
            frame.member_name = None
            frame.state = _S_COLON
            return None
        if role == _R_VALUE_SELECTED:
            frame.acc[frame.member_name] = scan.value
        frame.member_name = None
        frame.member_selected = False
        frame.state = _S_COMMA
        return None

    def _close(self):
        frame = self.frames.pop()
        result = None
        if frame.kind == _RECORD:
            acc = frame.acc
            result = {}
            for name in self.fields:
                if name in acc:
                    result[name] = acc[name]
            frame.acc = None
        elif frame.kind == _TOP:
            if frame.records_count != 1:
                raise ValueError(
                    "the top-level object must contain exactly one "
                    "'records' member")
            self.root_done = True
        if self.frames:
            parent = self.frames[-1]
            parent.state = _S_COMMA
            parent.member_name = None
            parent.member_selected = False
        return result

    # -- end of input ----------------------------------------------------
    def finish(self):
        if self.pending is not None:
            raise ValueError(
                "unexpected end of data inside a JSON token")
        if self.frames:
            raise ValueError(
                "unexpected end of data: unterminated JSON value")
        if not self.root_done:
            raise ValueError("empty document: no top-level value")


def _skip_ws(buf, pos, n):
    while pos < n and buf[pos] in _WS:
        pos += 1
    return pos


def _plain_end(buf, pos, n):
    """Index of the closing quote of a plain string, or -1 if it needs work.

    The opening quote has already been consumed.  A string is "plain" when it
    has no backslash and no control character before its closing quote, so its
    text is exactly ``buf[pos:end]``.
    """
    match = _STOP.search(buf, pos, n)
    if match is None:
        return -1
    if buf[match.start()] == '"':
        return match.start()
    return -1


def _validate_fields(fields):
    if isinstance(fields, (str, bytes)):
        raise TypeError("fields must be a sequence of names, not a string")
    try:
        names = list(fields)
    except TypeError:
        raise TypeError("fields must be an iterable of names") from None
    if not 1 <= len(names) <= MAX_FIELDS:
        raise ValueError("fields must contain between 1 and 16 names")
    for name in names:
        if not isinstance(name, str):
            raise TypeError("field names must be strings")
    if len(set(names)) != len(names):
        raise ValueError("fields must be distinct")
    return names


def _iterate(chunks, names):
    parser = _Parser(names)
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    for chunk in chunks:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("chunks must yield bytes-like objects")
        try:
            text = decoder.decode(bytes(chunk))
        except UnicodeDecodeError as exc:
            raise ValueError("invalid UTF-8 in the input") from exc
        if text:
            yield from parser.feed(text)
    try:
        tail = decoder.decode(b"", True)
    except UnicodeDecodeError as exc:
        raise ValueError("truncated UTF-8 sequence at end of input") from exc
    if tail:
        yield from parser.feed(tail)
    parser.finish()


def rows(chunks, fields):
    """Lazily yield the projection of every record in an RFC 8259 document.

    ``chunks`` is an iterable of ``bytes`` objects; each chunk is at most
    32 KiB and may split a character, a token or the document anywhere.
    ``fields`` is a list of 1 to 16 distinct names.  Each yielded value is a
    new ``dict`` holding one key per selected field that occurs in the record;
    the record's closing brace must have been consumed before it is yielded.

    Errors (syntax, UTF-8, truncation, structure) raise :class:`ValueError`
    while iterating.  Records yielded before the error stay yielded.
    """
    names = _validate_fields(fields)
    return _iterate(chunks, names)
