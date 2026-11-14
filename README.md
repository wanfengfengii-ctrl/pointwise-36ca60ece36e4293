# exportstream

`exportstream.rows(chunks, fields)` walks a JSON export one chunk at a time and
yields one small, projected `dict` per record. The whole document is never
materialised: values that are not selected are parsed for validity and dropped
immediately, so neither skipped payloads nor the number of records add to
retained memory.

Only the Python standard library is used (`re`, `codecs`, `json` in tests and
tools).

## API

```python
from exportstream import rows

chunks = iter([b'{"records":[{"id":1},{"id":null},{}]}'])
list(rows(chunks, ["id"]))
# [{'id': 1}, {'id': None}, {}]
```

* `chunks` — an iterable of a single bytes stream. Each chunk is at most
  32 KiB, but the implementation does not depend on that: every chunk size
  from 1 byte upward is accepted. Chunks may split a UTF-8 character, a token
  or the document at any byte.
* `fields` — a list of 1 to 16 distinct strings. Validation is eager:
  `rows` raises `TypeError`/`ValueError` before it pulls the first chunk.
* Return value — a lazy generator. Each item is a freshly built `dict` with
  one key per selected field present in the record.

## Projection contract

| Input | Output |
| --- | --- |
| `{"id": 1}`, fields `["id"]` | `{"id": 1}` |
| `{"id": null}` | `{"id": null}` (null is kept) |
| `{}` | `{}` (missing fields are omitted) |
| `{"id": 1, "id": 2}` | `{"id": 2}` (last duplicate wins) |
| `{"b": 1, "a": 2}` | key order follows `fields`, not the document |

* Records are delivered only after their closing `}` has been consumed, so a
  document cut off mid-record never yields a partial row. Everything already
  yielded stays yielded when a later `ValueError` is raised.
* The top-level value must be an object with exactly one `records` member
  (duplicate `records` names are rejected). `records` must be an array whose
  elements are all objects. Other members may hold any JSON value.
* A selected value must be a string, signed 64-bit integer, boolean or null.
  Floats, arrays and objects in a selected field are errors; unselected values
  are unrestricted.
* `records: []` yields nothing.
* Nesting must stay within 32 levels including the root object
  (RFC 8259 §9).
* Documents without a BOM are required; a BOM is a syntax error.

Errors — invalid syntax, invalid or truncated UTF-8, truncation, wrong
structure, duplicate `records`, nesting beyond 32 levels, an over-large
projection, and a second top-level value — raise `ValueError`.

## Deliverables

### 1. Reference projection

```
$ python3 -c 'from demo.demo import show_reference; show_reference()'
  input : {"records": [{"id": 1}, {"id": null}, {}]}
  rows  : [{"id": 1}, {"id": null}, {}]
```

### 2. Chunk differential

`tests/test_chunking.py` re-runs a document containing escapes, multi-byte
characters, long tokens and skipped nesting at every chunk size 1..96 and at
every single two-way split point, asserting the rows equal both the one-shot
list and `json.loads`. `python3 -m demo.demo` prints a summary of 11 chunk
sizes.

### 3. Bad-package verification

`tests/test_errors.py` covers truncation at every byte, trailing commas in
every container, missing separators, bad literals/numbers/escapes, control
characters, invalid UTF-8, a BOM, duplicate `records`, deep nesting,
out-of-range integers, wrong selected types and trailing data. Each case
asserts `ValueError` and that the already-delivered prefix is unchanged.
E.g. `{"records":[{"id":1}],"skip":[1,]}` raises after `{"id": 1}` was
delivered.

### 4. State and memory

* The only per-document state is: one frame per open container (≤ 32), the
  current chunk plus the unparsed tail, and a scanner object for a token that
  is split by a chunk boundary. A split token keeps bounded state — at most
  64 KiB of characters for a selected string, ≤ 20 digits for a number, six
  characters for a literal, and none at all for strings outside the
  projection (only their length is tracked).
* Selected values are placed into the record dict; they are never copied into
  the document buffer. A record is capped at 64 KiB of `json.dumps` output,
  which bounds the retained selected data.
* `python3 verify.py` streams 64 MiB of skipped string followed by 50 000
  records and asserts a peak traced-allocation delta ≤ 8 MiB (measured
  ≈ 0.1–0.9 MiB, independent of payload and record count).

## Commands

```
python3 -m unittest discover -s tests -t .   # 49 tests, including memory checks
python3 verify.py                            # tests + memory + reference checks
python3 -m demo.demo                         # reference, differential, bad input, state
```

`.pointwise-runtime.json` lists these as `setup`, `verify` and `demo`.
