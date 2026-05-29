"""Minimal YAML subset for subjob — stdlib-only.

Supported:
  - Block mappings (`key: value`, nested by indent)
  - Block sequences (`- item`, nested by indent)
  - Inline flow lists: `[]`, `[a, b, "c"]`
  - Empty inline flow map: `{}`
  - Block scalars: `|` (literal, clip), `|-` (literal, strip), `|+` (tolerated as clip)
  - Scalars: int, float, bool (`true`/`false`), null (`null`, `~`, empty),
    plain strings, single-quoted, double-quoted (with `\\n`, `\\t`, `\\\\`, `\\"`)
  - Comments: full-line `#…` and end-of-line `␣#…` on plain scalars
  - List-of-dicts via `- key: value` opener

NOT supported (raises ParseError):
  - Anchors `&` / aliases `*`
  - Multi-document streams `---`
  - Folded scalars `>`
  - Non-empty flow mappings `{a: b}`
  - Tabs for indentation
  - Type tags `!!str`
"""

from __future__ import annotations

import re
from typing import Any


class ParseError(ValueError):
    """Raised when input uses an unsupported YAML feature or is malformed."""


_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d*(?:[eE][+-]?\d+)?$|^-?\d+[eE][+-]?\d+$")
_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_\-]*):(?:\s+(.*))?$|^([A-Za-z_][A-Za-z0-9_\-]*):\s*$")
_SAFE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")


def loads(text: str) -> Any:
    """Parse YAML text. Returns a dict, list, scalar, or None."""
    lines = _prepare(text)
    if not lines:
        return None
    value, idx = _parse_block(lines, 0, -1)
    # Anything left over at column 0 that we didn't consume is a structural error.
    while idx < len(lines):
        ln_no, indent, content = lines[idx]
        raise ParseError(f"line {ln_no}: unexpected content at indent {indent}: {content!r}")
    return value


def dumps(obj: Any) -> str:
    """Serialize obj to YAML. Output is parseable by loads()."""
    out: list[str] = []
    _emit(obj, 0, out, is_root=True)
    return "\n".join(out) + ("\n" if out else "")


# ---------- preprocessing ----------


def _prepare(text: str) -> list[tuple[int, int, str]]:
    """Strip blank/comment lines, return (line_no, indent, content) tuples.

    Block scalar contents are NOT stripped here; we detect `|` during parsing
    and slurp the following lines with their raw indentation preserved.
    """
    out: list[tuple[int, int, str]] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip(" \t"))]:
            raise ParseError(f"line {line_no}: tabs are not allowed for indentation")
        stripped = raw.lstrip(" ")
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("---") or stripped.startswith("..."):
            raise ParseError(f"line {line_no}: multi-document streams are not supported")
        if stripped.startswith("&") or stripped.startswith("*"):
            raise ParseError(f"line {line_no}: anchors/aliases are not supported")
        indent = len(raw) - len(stripped)
        out.append((line_no, indent, stripped))
    return out


# ---------- parsing ----------


def _parse_block(lines, i, parent_indent):
    """Parse a value at lines[i]. Returns (value, next_index).

    Mappings must be at indent > parent_indent. Block sequences may live at
    the same indent as their parent key (idiomatic YAML), so we accept
    indent >= parent_indent when the first character is a list dash.
    """
    if i >= len(lines):
        return None, i
    _, indent, content = lines[i]
    is_seq = content == "-" or content.startswith("- ")
    if is_seq:
        if indent < parent_indent:
            return None, i
        return _parse_sequence(lines, i, indent)
    if indent <= parent_indent:
        return None, i
    return _parse_mapping(lines, i, indent)


def _parse_mapping(lines, i, indent):
    result: dict[str, Any] = {}
    while i < len(lines):
        ln_no, ln_indent, content = lines[i]
        if ln_indent < indent:
            break
        if ln_indent > indent:
            raise ParseError(f"line {ln_no}: unexpected indentation in mapping")
        m = _KEY_RE.match(content)
        if not m:
            raise ParseError(f"line {ln_no}: expected 'key: value', got: {content!r}")
        key = m.group(1) or m.group(3)
        rhs = m.group(2)  # may be None
        i += 1
        if rhs is None or rhs == "":
            # Value is on the following lines, deeper-indented, OR null.
            value, i = _parse_block(lines, i, indent)
        elif rhs in ("|", "|-", "|+"):
            value, i = _slurp_block_scalar(lines, i, indent, chomp=rhs[1:])
        else:
            value = _parse_scalar(rhs, ln_no)
        result[key] = value
    return result, i


def _parse_sequence(lines, i, indent):
    items: list[Any] = []
    while i < len(lines):
        ln_no, ln_indent, content = lines[i]
        if ln_indent < indent:
            break
        if ln_indent > indent:
            raise ParseError(f"line {ln_no}: unexpected indentation in sequence")
        if not content.startswith("-"):
            break
        # Strip leading "- " or "-"
        if content == "-":
            rest = ""
        elif content.startswith("- "):
            rest = content[2:]
        else:
            raise ParseError(f"line {ln_no}: invalid sequence entry: {content!r}")
        i += 1
        if rest == "":
            value, i = _parse_block(lines, i, indent)
            items.append(value)
            continue
        # `- key: value` is a list-of-dicts opener.
        if _KEY_RE.match(rest):
            # Treat this rest as the first key of a mapping that lives at indent+2.
            # Simulate by injecting a synthetic line. We do this by recursing on a
            # subset: parse this single line plus any following lines whose indent
            # is at least indent+2.
            entry_indent = indent + 2
            synth = [(ln_no, entry_indent, rest)]
            # Collect continuation lines.
            while i < len(lines) and lines[i][1] >= entry_indent:
                synth.append(lines[i])
                i += 1
            value, j = _parse_mapping(synth, 0, entry_indent)
            if j != len(synth):
                _, _, leftover = synth[j]
                raise ParseError(f"line {ln_no}: malformed list-of-dicts entry near {leftover!r}")
            items.append(value)
            continue
        # Scalar (possibly an inline list).
        items.append(_parse_scalar(rest, ln_no))
    return items, i


def _slurp_block_scalar(lines, i, key_indent, chomp=""):
    """Collect raw lines indented deeper than `key_indent` for a block scalar.

    Preserves newlines, strips the leading common indent. The `chomp` indicator
    controls the trailing newline (matching the emitter's chomping choice):
      ""  (`|`)  → clip:  keep exactly one trailing "\\n"
      "-" (`|-`) → strip: no trailing newline
      "+" (`|+`) → keep:  treated as clip here (Phase-0: we never emit `|+`,
                   and our blocks don't carry trailing blank lines to keep).
    """
    if i >= len(lines):
        return "", i
    # The block's indent is whatever the first content line has.
    block_indent = lines[i][1]
    if block_indent <= key_indent:
        return "", i
    chunks: list[str] = []
    while i < len(lines):
        ln_no, ln_indent, content = lines[i]
        if ln_indent < block_indent:
            break
        # `content` already has its leading whitespace stripped; rebuild with
        # the extra-indent (relative to block_indent) preserved.
        extra = ln_indent - block_indent
        chunks.append(" " * extra + content)
        i += 1
    body = "\n".join(chunks)
    if chomp == "-":
        return body, i
    return body + "\n", i


# ---------- scalar parsing ----------


def _parse_scalar(text: str, line_no: int) -> Any:
    s = _strip_trailing_comment(text).strip()
    if s.startswith("&") or s.startswith("*"):
        raise ParseError(f"line {line_no}: anchors/aliases are not supported")
    if s.startswith("!"):
        raise ParseError(f"line {line_no}: explicit type tags are not supported")
    if s == "" or s == "null" or s == "~":
        return None
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if s.startswith('"'):
        return _parse_double_quoted(s, line_no)
    if s.startswith("'"):
        return _parse_single_quoted(s, line_no)
    if s.startswith("["):
        return _parse_flow_list(s, line_no)
    if s.startswith("{"):
        if s == "{}":
            return {}
        raise ParseError(f"line {line_no}: non-empty flow mappings not supported: {s!r}")
    if s.startswith(">"):
        raise ParseError(f"line {line_no}: folded scalars (>) not supported")
    if _INT_RE.match(s):
        return int(s)
    if _FLOAT_RE.match(s):
        return float(s)
    return s  # plain string


def _strip_trailing_comment(s: str) -> str:
    in_single = False
    in_double = False
    i = 0
    while i < len(s):
        c = s[i]
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            if i == 0 or s[i - 1] != "\\":
                in_double = not in_double
        elif c == "#" and not in_single and not in_double:
            if i == 0 or s[i - 1] == " ":
                return s[:i].rstrip()
        i += 1
    return s


def _parse_double_quoted(s: str, line_no: int) -> str:
    if not s.endswith('"') or len(s) < 2:
        raise ParseError(f"line {line_no}: unterminated double-quoted string")
    body = s[1:-1]
    out: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}.get(nxt, nxt))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _parse_single_quoted(s: str, line_no: int) -> str:
    if not s.endswith("'") or len(s) < 2:
        raise ParseError(f"line {line_no}: unterminated single-quoted string")
    return s[1:-1].replace("''", "'")


def _parse_flow_list(s: str, line_no: int) -> list:
    if not s.endswith("]"):
        raise ParseError(f"line {line_no}: unterminated flow list: {s!r}")
    inner = s[1:-1].strip()
    if inner == "":
        return []
    # Split on commas not inside quotes.
    parts: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    for c in inner:
        if c == "'" and not in_double:
            in_single = not in_single
            buf.append(c)
        elif c == '"' and not in_single:
            in_double = not in_double
            buf.append(c)
        elif c == "," and not in_single and not in_double:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(c)
    if buf:
        parts.append("".join(buf).strip())
    return [_parse_scalar(p, line_no) for p in parts]


# ---------- emitting ----------


_SAFE_PLAIN_RE = re.compile(r"^[A-Za-z_/][A-Za-z0-9_/.\-+@]*$")


def _emit(value, indent, out, is_root=False, after_dash=False):
    """Append YAML for `value` to `out`. `indent` is spaces for nested children."""
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            if not is_root:
                # Same-line emission handled by caller; just write {}.
                out[-1] = out[-1] + " {}"
            else:
                out.append("{}")
            return
        keys = list(value)
        for k_i, k in enumerate(keys):
            v = value[k]
            if not isinstance(k, str) or not _SAFE_KEY_RE.match(k):
                raise ValueError(f"unsafe mapping key: {k!r}")
            prefix = pad if not (after_dash and k_i == 0) else ""
            if isinstance(v, dict):
                if not v:
                    out.append(f"{prefix}{k}: {{}}")
                else:
                    out.append(f"{prefix}{k}:")
                    _emit(v, indent + 2, out)
            elif isinstance(v, list):
                if not v:
                    out.append(f"{prefix}{k}: []")
                else:
                    out.append(f"{prefix}{k}:")
                    _emit(v, indent, out)  # sequences live at same indent as parent key
            elif isinstance(v, str) and "\n" in v:
                # Multi-line strings use the literal block scalar form, with a
                # chomping indicator so the trailing-newline state round-trips:
                #   ends with exactly one "\n" → `|`  (clip: keep one newline)
                #   otherwise                  → `|-` (strip: no trailing newline)
                if v.endswith("\n") and not v.endswith("\n\n"):
                    header = "|"
                    body = v[:-1]  # drop the single trailing newline before splitting
                else:
                    header = "|-"
                    body = v
                out.append(f"{prefix}{k}: {header}")
                for ln in body.split("\n"):
                    out.append(f"{pad}  {ln}")
            else:
                out.append(f"{prefix}{k}: {_emit_scalar(v)}")
    elif isinstance(value, list):
        if not value:
            out.append(f"{pad}[]")
            return
        for item in value:
            if isinstance(item, dict):
                out.append(f"{pad}-")
                # rewrite last line to attach first key after dash
                out.pop()
                out.append(f"{pad}- __PLACEHOLDER__")
                # produce the dict at indent+2 then splice
                child_lines: list[str] = []
                _emit(item, indent + 2, child_lines)
                if child_lines:
                    out[-1] = f"{pad}- " + child_lines[0].lstrip()
                    out.extend(child_lines[1:])
                else:
                    out[-1] = f"{pad}- {{}}"
            elif isinstance(item, list):
                raise ValueError("nested lists in block style not supported by yaml_lite emitter")
            else:
                out.append(f"{pad}- {_emit_scalar(item)}")
    else:
        out.append(f"{pad}{_emit_scalar(value)}")


def _emit_scalar(v) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        if v == "" or v in ("null", "true", "false", "~"):
            return f'"{v}"'
        if _SAFE_PLAIN_RE.match(v) and not _INT_RE.match(v) and not _FLOAT_RE.match(v):
            return v
        # Quote with double-quotes, escape as needed.
        esc = v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
        return f'"{esc}"'
    raise ValueError(f"unsupported scalar type for yaml_lite: {type(v).__name__}")
