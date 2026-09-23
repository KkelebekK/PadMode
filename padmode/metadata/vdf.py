"""Reading Valve's text KeyValues format (``.vdf``, ``.acf``).

Written by hand because the alternative is a dependency, and the project
promises stdlib only (CLAUDE.md §2). The format is small: quoted or bare
strings, braces for nesting, ``//`` comments. Every value is a string or a
nested block; nothing is typed, so callers convert what they need.

Only the *text* format. ``appinfo.vdf``, ``packageinfo.vdf`` and
``shortcuts.vdf`` share the extension but are binary, and are not read here.

Three liberties, all matching how Steam itself reads these files:

- **Keys come back lowercased.** KeyValues looks keys up without regard to
  case, and Steam has not always written them the same way -- the root of
  ``libraryfolders.vdf`` has been both ``LibraryFolders`` and
  ``libraryfolders``. Lowercasing once here means no caller has to guess.
  Values are untouched: they hold paths and names.
- A repeated key keeps its last value. KeyValues allows duplicates, but
  nothing we read uses them, and a dict is a much easier thing to hand back.
- Platform conditionals (``"key" "value" [$WIN32]``) are dropped rather than
  evaluated. None of the files we read carry them; if one ever does, dropping
  the tag keeps the parse aligned instead of reading ``[$WIN32]`` as a key.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

#: What a backslash turns into inside a quoted string. Anything else keeps its
#: backslash, which is what a Windows path written without escaping needs.
ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}

#: Characters that end a bare (unquoted) token.
_BREAKS = frozenset('{}"')


class VDFError(ValueError):
    """The text is not KeyValues. Carries the line it gave up on."""

    def __init__(self, message: str, line: int) -> None:
        super().__init__(f"line {line}: {message}")
        self.line = line


def loads(text: str) -> dict:
    """Parse KeyValues text into nested dicts of strings, keys lowercased.

    A leading byte-order mark is dropped. Steam ships files that start with
    one (its controller templates, measured), and a BOM is not whitespace to
    Python, so left in it reads as a bare key and shifts every pair after it.
    """
    return _block(_tokens(text.removeprefix("\ufeff")), closed_by_brace=False)


def load(path: Path) -> dict:
    """Parse a KeyValues file. Steam writes UTF-8; a stray byte will not stop us."""
    return loads(path.read_text(encoding="utf-8", errors="replace"))


def _block(tokens: Iterator[tuple[str, str, int]], *, closed_by_brace: bool) -> dict:
    result: dict = {}
    line = 1
    for kind, key, line in tokens:
        if kind == "}":
            if closed_by_brace:
                return result
            raise VDFError("unexpected '}'", line)
        if kind == "{":
            raise VDFError("a block needs a key before it", line)
        if kind == "cond":
            continue
        kind, value, line = next(tokens, ("eof", "", line))
        while kind == "cond":
            kind, value, line = next(tokens, ("eof", "", line))
        if kind == "{":
            result[key.lower()] = _block(tokens, closed_by_brace=True)
        elif kind == "str":
            result[key.lower()] = value
        else:
            raise VDFError(f"key {key!r} has no value", line)
    if closed_by_brace:
        raise VDFError("a block is never closed", line)
    return result


def _tokens(text: str) -> Iterator[tuple[str, str, int]]:
    """``(kind, text, line)``. Kinds: ``str``, ``{``, ``}``, ``cond``."""
    i, line, end = 0, 1, len(text)
    while i < end:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif c.isspace():
            i += 1
        elif text.startswith("//", i):
            newline = text.find("\n", i)
            i = end if newline < 0 else newline
        elif c in "{}":
            yield c, c, line
            i += 1
        elif c == '"':
            start_line = line
            chars: list[str] = []
            i += 1
            while True:
                if i >= end:
                    raise VDFError("a quoted string is never closed", start_line)
                c = text[i]
                if c == '"':
                    i += 1
                    break
                if c == "\\" and i + 1 < end and text[i + 1] in ESCAPES:
                    chars.append(ESCAPES[text[i + 1]])
                    i += 2
                    continue
                if c == "\n":
                    line += 1
                chars.append(c)
                i += 1
            yield "str", "".join(chars), start_line
        else:
            start = i
            while i < end and not text[i].isspace() and text[i] not in _BREAKS:
                i += 1
            word = text[start:i]
            if word.startswith("[") and word.endswith("]"):
                yield "cond", word, line
            else:
                yield "str", word, line
