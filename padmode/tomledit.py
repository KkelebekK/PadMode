"""Edit a TOML file in place, keeping everything the user wrote around the edit.

The standard library reads TOML and cannot write it. The settings window
(v0.8) has to write the config back, and a regenerated file would lose every
comment the user put in it -- so this changes only the text that has to
change: a value is rewritten where it stands, a missing key goes at the end of
its table, a missing table at the end of the file. Comments, order, blank lines
and the user's own formatting of everything else survive.

This is **not a TOML writer**. It understands just enough of the syntax to
find where each statement starts and ends, and it writes only the values our
schema uses: booleans, integers, floats, strings and lists of those.

The safety net is not the lexer being right. **Every edit is re-read with
``tomllib`` and compared to what it was meant to produce** -- the old document
with exactly that one change -- before it is accepted. A file written in a
shape this cannot edit cleanly (an inline table, dotted keys, an array of
tables) fails that comparison or is refused up front, and raises
:class:`EditError`; the text is left exactly as it was. Nothing here ever
writes a file: :func:`padmode.config.write` does, after its own checks.
"""

from __future__ import annotations

import copy
import math
import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")

#: TOML basic-string escapes, both ways.
_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r",
            '"': '"', "\\": "\\", "e": "\x1b"}
_UNESCAPES = {"\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r",
              '"': '\\"', "\\": "\\\\"}


class EditError(Exception):
    """The edit could not be made without risking the rest of the file."""


@dataclass(frozen=True)
class _Header:
    path: tuple[str, ...]
    start: int      # first character of the header's line
    end: int        # just past the newline that ends it


@dataclass(frozen=True)
class _Pair:
    table: tuple[str, ...]
    key: tuple[str, ...]   # dotted keys give more than one part
    start: int             # first character of the line
    value_start: int
    value_end: int
    end: int               # just past the newline after the value


class Document:
    """A TOML text and the edits made to it so far."""

    def __init__(self, text: str) -> None:
        try:
            self._data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise EditError(f"not valid TOML: {exc}") from exc
        self._text = text
        self._newline = "\r\n" if "\r\n" in text else "\n"
        self._headers, self._pairs = _scan(text)

    @property
    def text(self) -> str:
        return self._text

    @property
    def data(self) -> dict[str, Any]:
        """The parsed document, as ``tomllib`` reads it. A copy."""
        return copy.deepcopy(self._data)

    # -- edits ---------------------------------------------------------
    def set(self, table: Sequence[str], key: str, value: Any) -> None:
        """Make ``table.key`` equal ``value``, writing as little as possible."""
        table = tuple(table)
        rendered = render(value)
        if isinstance(value, tuple):
            value = list(value)
        current = _lookup(self._data, table + (key,))
        if type(current) is type(value) and current == value:
            return
        pair = self._pair(table, key)
        if pair is not None:
            text = (self._text[:pair.value_start] + rendered
                    + self._text[pair.value_end:])
        else:
            text = self._insert(table, f"{render_key(key)} = {rendered}")
        expected = self.data
        _assign(expected, table + (key,), value)
        self._commit(text, expected)

    def remove(self, table: Sequence[str], key: str) -> bool:
        """Delete ``table.key``, so the default applies. False if it was absent."""
        table = tuple(table)
        if _lookup(self._data, table + (key,)) is _MISSING:
            return False
        pair = self._pair(table, key)
        if pair is None:
            raise EditError(
                f"{_name(table + (key,))} is set in a form this cannot edit "
                "(an inline table or a dotted key)"
            )
        text = self._text[:pair.start] + self._text[pair.end:]
        expected = self.data
        _delete(expected, table + (key,))
        self._commit(text, expected)
        # A section left with nothing in it -- no keys, no sub-tables, not even
        # a comment -- goes too. Otherwise setting a value and putting it back
        # would leave an empty [header] behind, and the file would differ from
        # what it was although nothing in it means anything different.
        if table and _lookup(self._data, table) == {}:
            header = next((h for h in self._headers if h.path == table), None)
            if header is not None:
                later = [h.start for h in self._headers if h.start > header.start]
                end = min(later) if later else len(self._text)
                if not self._text[header.end:end].strip():
                    self.remove_table(table)
        return True

    def remove_table(self, table: Sequence[str]) -> bool:
        """Delete a whole ``[table]`` section. False if there was none.

        Only the section itself goes -- its header and the lines under it, up
        to the next header. Sub-tables written under their own headers are not
        touched; nothing in our schema nests a table we would delete.
        """
        table = tuple(table)
        if _lookup(self._data, table) is _MISSING:
            return False
        header = next((h for h in self._headers if h.path == table), None)
        if header is None:
            raise EditError(f"[{_name(table)}] is not written as its own section")
        later = [h.start for h in self._headers if h.start > header.start]
        end = min(later) if later else len(self._text)
        text = self._text[:header.start] + self._text[end:]
        expected = self.data
        _delete(expected, table)
        # Parents that only existed because this section named them go too:
        # [metadata.steam.ignore] alone is the whole of [metadata].
        for k in range(len(table) - 1, 0, -1):
            parent = table[:k]
            if _lookup(expected, parent) == {} and not any(
                h.path == parent for h in self._headers
            ):
                _delete(expected, parent)
        # A section whose sub-tables live elsewhere still exists implicitly.
        for h in self._headers:
            if h.path[:len(table)] == table and h.path != table:
                expected_sub = _lookup(self._data, h.path)
                _assign(expected, h.path, copy.deepcopy(expected_sub))
        self._commit(_tidy_join(text, header.start, self._newline), expected)
        return True

    # -- internals -----------------------------------------------------
    def _pair(self, table: tuple[str, ...], key: str) -> _Pair | None:
        target = table + (key,)
        found = None
        for pair in self._pairs:
            full = pair.table + pair.key
            if full == target:
                found = pair
            elif full[:len(target)] == target or target[:len(full)] == full:
                # A dotted key or inline table overlapping what we want to
                # touch: editing around it would be guesswork.
                raise EditError(
                    f"{_name(target)} is written as part of "
                    f"{_name(full)}; edit that by hand"
                )
        return found

    def _insert(self, table: tuple[str, ...], line: str) -> str:
        nl = self._newline
        header = next((h for h in self._headers if h.path == table), None)
        if header is None and table:
            text = self._text
            if text and not text.endswith(("\n", "\r\n")):
                text += nl
            if text.strip():
                text += nl
            return f"{text}[{_name(table)}]{nl}{line}{nl}"
        # Inside an existing section (or the top level): after the last
        # statement that belongs to it, matching its indentation.
        if header is None:
            section_start = 0
            later = [h.start for h in self._headers]
        else:
            section_start = header.end
            later = [h.start for h in self._headers if h.start > header.start]
        section_end = min(later) if later else len(self._text)
        members = [p for p in self._pairs
                   if section_start <= p.start < section_end]
        if members:
            last = members[-1]
            at = last.end
            line = _indent_of(self._text, last.start) + line
        else:
            at = section_start
        before = self._text[:at]
        if before and not before.endswith(("\n", "\r\n")):
            before += nl
        return before + line + nl + self._text[at:]

    def _commit(self, text: str, expected: dict[str, Any]) -> None:
        try:
            got = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise EditError(f"the edit would break the file: {exc}") from exc
        if got != expected:
            raise EditError(
                "the edit would change more than it should; the file is "
                "written in a shape this cannot edit safely"
            )
        self._text = text
        self._data = got
        self._headers, self._pairs = _scan(text)


# -- rendering ------------------------------------------------------------
def render(value: Any) -> str:
    """A value as TOML. Only the kinds our schema uses."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EditError(f"cannot write {value!r}")
        text = repr(value)
        return text if any(c in text for c in ".e") else text + ".0"
    if isinstance(value, str):
        return _basic_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(render(item) for item in value) + "]"
    raise EditError(f"cannot write a {type(value).__name__}")


def render_key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else _basic_string(key)


def _basic_string(value: str) -> str:
    out = []
    for ch in value:
        if ch in _UNESCAPES:
            out.append(_UNESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _name(path: Sequence[str]) -> str:
    return ".".join(render_key(part) for part in path)


# -- the scanner ------------------------------------------------------------
def _scan(text: str) -> tuple[list[_Header], list[_Pair]]:
    """Where every header and key/value pair starts and ends.

    Run only on text ``tomllib`` has already accepted, so it can assume the
    syntax is valid and concentrate on boundaries.
    """
    headers: list[_Header] = []
    pairs: list[_Pair] = []
    table: tuple[str, ...] = ()
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "#":
            i = _line_end(text, i)
            continue
        start = _line_start(text, i)
        if ch == "[":
            if text.startswith("[[", i):
                raise EditError("arrays of tables ([[...]]) are not supported")
            path, j = _key_path(text, i + 1)
            j = _skip_space(text, j)
            assert text[j] == "]"
            end = _after_line(text, j + 1)
            headers.append(_Header(path, start, end))
            table = path
            i = end
            continue
        key, j = _key_path(text, i)
        j = _skip_space(text, j)
        assert text[j] == "="
        value_start = _skip_space(text, j + 1)
        value_end = _value_end(text, value_start)
        end = _after_line(text, value_end)
        pairs.append(_Pair(table, key, start, value_start, value_end, end))
        i = end
    return headers, pairs


def _key_path(text: str, i: int) -> tuple[tuple[str, ...], int]:
    parts = []
    while True:
        i = _skip_space(text, i)
        if text[i] == '"':
            end = _basic_end(text, i)
            parts.append(_decode_basic(text[i + 1:end - 1]))
            i = end
        elif text[i] == "'":
            end = text.index("'", i + 1) + 1
            parts.append(text[i + 1:end - 1])
            i = end
        else:
            match = _BARE_KEY.match(text, i)
            assert match is not None
            parts.append(match.group())
            i = match.end()
        j = _skip_space(text, i)
        if j < len(text) and text[j] == ".":
            i = j + 1
            continue
        return tuple(parts), i


def _value_end(text: str, i: int) -> int:
    """Just past the value that starts at ``i``."""
    if text.startswith('"""', i):
        return _multi_basic_end(text, i)
    if text.startswith("'''", i):
        j = text.index("'''", i + 3)
        while text.startswith("'", j + 3):  # up to two quotes may end it
            j += 1
        return j + 3
    if text[i] == '"':
        return _basic_end(text, i)
    if text[i] == "'":
        return text.index("'", i + 1) + 1
    if text[i] in "[{":
        return _bracket_end(text, i)
    j = i
    while j < len(text) and text[j] not in "#\r\n":
        j += 1
    return len(text[i:j].rstrip()) + i


def _bracket_end(text: str, i: int) -> int:
    depth = 0
    j = i
    while j < len(text):
        ch = text[j]
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return j + 1
        elif ch == "#":
            j = _line_end(text, j)
            continue
        elif ch in "\"'":
            j = _value_end(text, j)
            continue
        j += 1
    raise EditError("unbalanced brackets")


def _basic_end(text: str, i: int) -> int:
    j = i + 1
    while text[j] != '"':
        j += 2 if text[j] == "\\" else 1
    return j + 1


def _multi_basic_end(text: str, i: int) -> int:
    j = i + 3
    while True:
        if text[j] == "\\":
            j += 2
        elif text.startswith('"""', j):
            k = j + 3
            while k < len(text) and text[k] == '"' and k - j < 5:
                k += 1  # up to two quotes may end the string itself
            return k
        else:
            j += 1


def _decode_basic(raw: str) -> str:
    out, i = [], 0
    while i < len(raw):
        ch = raw[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        code = raw[i + 1]
        if code in _ESCAPES:
            out.append(_ESCAPES[code])
            i += 2
        elif code == "u":
            out.append(chr(int(raw[i + 2:i + 6], 16)))
            i += 6
        elif code == "U":
            out.append(chr(int(raw[i + 2:i + 10], 16)))
            i += 10
        else:
            raise EditError(f"unknown escape \\{code}")
    return "".join(out)


def _skip_space(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t":
        i += 1
    return i


def _line_start(text: str, i: int) -> int:
    return text.rfind("\n", 0, i) + 1


def _line_end(text: str, i: int) -> int:
    j = text.find("\n", i)
    return len(text) if j < 0 else j


def _after_line(text: str, i: int) -> int:
    """Past the end of the line ``i`` is on, newline included."""
    j = text.find("\n", i)
    return len(text) if j < 0 else j + 1


def _indent_of(text: str, line_start: int) -> str:
    j = line_start
    while j < len(text) and text[j] in " \t":
        j += 1
    return text[line_start:j]


def _tidy_join(text: str, at: int, nl: str) -> str:
    """One blank line where a section was cut out -- not none, not three."""
    before = text[:at].rstrip("\r\n")
    after = text[at:].lstrip("\r\n")
    if not before:
        return after
    if not after:
        return before + nl
    return before + nl + nl + after


# -- dict helpers -----------------------------------------------------------
_MISSING = object()


def _lookup(data: dict, path: Sequence[str]) -> Any:
    node: Any = data
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _assign(data: dict, path: Sequence[str], value: Any) -> None:
    node = data
    for part in path[:-1]:
        node = node.setdefault(part, {})
    if isinstance(value, tuple):
        value = list(value)
    node[path[-1]] = copy.deepcopy(value)


def _delete(data: dict, path: Sequence[str]) -> None:
    node = data
    for part in path[:-1]:
        node = node[part]
    del node[path[-1]]
