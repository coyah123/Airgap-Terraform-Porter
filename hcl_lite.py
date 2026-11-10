"""
hcl_lite — just enough HCL2 parsing to read Terraform's structural blocks.

Not a full HCL implementation. It tokenizes the file properly (strings with
${...} interpolation, heredocs, all three comment styles), then parses the
body grammar:

    body      := (attribute | block)*
    attribute := IDENT "=" expression NEWLINE
    block     := IDENT (STRING | IDENT)* "{" body "}"

Expressions are only *evaluated* when they are a plain string literal or an
object/tuple of them (which is all `required_providers`, `required_version`,
`source`, and lock-file `hashes` ever are). Any other expression is captured as
`Raw` and skipped by bracket matching, so arbitrary Terraform code is parsed
safely without understanding it.

Standard library only.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class HCLError(ValueError):
    pass


@dataclass
class Raw:
    """An expression we didn't evaluate (function call, reference, etc.)."""
    text: str = ""


@dataclass
class Block:
    type: str
    labels: list[str]
    attrs: dict = field(default_factory=dict)
    blocks: list["Block"] = field(default_factory=list)
    line: int = 0

    def children(self, type_: str) -> list["Block"]:
        return [b for b in self.blocks if b.type == type_]


# -- tokenizer ---------------------------------------------------------------
# token = (kind, value, line). kinds: ident, string, number, heredoc, nl, punct, eof
_PUNCT = set("{}[]()=,:.?!<>+-*/%&|")


def tokenize(src: str):
    toks = []
    i, n, line = 0, len(src), 1

    def add(kind, val):
        toks.append((kind, val, line))

    while i < n:
        c = src[i]
        if c == "\n":
            add("nl", "\n")
            line += 1
            i += 1
        elif c in " \t\r﻿":
            i += 1
        elif c == "#" or src.startswith("//", i):
            while i < n and src[i] != "\n":
                i += 1
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += src.count("\n", i, j)
            i = j
        elif c == '"':
            val, i2 = _read_string(src, i)
            line += src.count("\n", i, i2)
            add("string", val)
            i = i2
        elif src.startswith("<<", i) and _heredoc_start(src, i):
            marker, body_start = _heredoc_start(src, i)
            # Body runs until a line whose stripped content == marker.
            j = body_start
            body_lines = []
            while j < n:
                k = src.find("\n", j)
                k = n if k < 0 else k
                ln = src[j:k]
                if ln.strip() == marker:
                    j = k
                    break
                body_lines.append(ln)
                j = k + 1
            line += src.count("\n", i, j)
            add("heredoc", "\n".join(body_lines))
            i = j
        elif c.isalpha() or c == "_":
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] in "_-"):
                j += 1
            add("ident", src[i:j])
            i = j
        elif c.isdigit():
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] in ".+-") and not (
                    src[j] in "+-" and src[j - 1] not in "eE"):
                j += 1
            add("number", src[i:j])
            i = j
        elif src.startswith("=>", i) or src.startswith("==", i) or \
                src.startswith("!=", i) or src.startswith(">=", i) or \
                src.startswith("<=", i) or src.startswith("&&", i) or \
                src.startswith("||", i) or src.startswith("...", i):
            w = 3 if src.startswith("...", i) else 2
            add("punct", src[i:i + w])
            i += w
        elif c in _PUNCT:
            add("punct", c)
            i += 1
        else:
            add("punct", c)       # unknown char; harmless as punctuation
            i += 1
    add("eof", "")
    return toks


def _heredoc_start(src: str, i: int):
    j = i + 2
    if j < len(src) and src[j] == "-":
        j += 1
    k = j
    while k < len(src) and (src[k].isalnum() or src[k] == "_"):
        k += 1
    if k == j:
        return None
    nl = src.find("\n", k)
    if nl < 0 or src[k:nl].strip():
        return None
    return src[j:k], nl + 1


def _read_string(src: str, i: int):
    """Read a quoted string starting at src[i]=='"'. Returns (value, end_index).
    Interpolations ${...} / %{...} are kept verbatim in the value."""
    assert src[i] == '"'
    out = []
    j = i + 1
    n = len(src)
    while j < n:
        c = src[j]
        if c == "\\" and j + 1 < n:
            nxt = src[j + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"',
                        "\\": "\\"}.get(nxt, "\\" + nxt))
            j += 2
        elif c == '"':
            return "".join(out), j + 1
        elif c in "$%" and src.startswith("{", j + 1) and not src.startswith(c * 2, j):
            k = _skip_template(src, j + 2)
            out.append(src[j:k])
            j = k
        elif c == "\n":
            raise HCLError("unterminated string")
        else:
            out.append(c)
            j += 1
    raise HCLError("unterminated string")


def _skip_template(src: str, j: int) -> int:
    """j is just after '${'. Return index just past the matching '}'."""
    depth = 1
    n = len(src)
    while j < n and depth:
        c = src[j]
        if c == '"':
            _, j = _read_string(src, j)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        j += 1
    return j


# -- parser ------------------------------------------------------------------
_OPEN = {"{": "}", "[": "]", "(": ")"}


class _Parser:
    def __init__(self, toks):
        self.t = toks
        self.i = 0

    def peek(self, off=0):
        return self.t[min(self.i + off, len(self.t) - 1)]

    def next(self):
        tok = self.t[self.i]
        self.i = min(self.i + 1, len(self.t) - 1)
        return tok

    def skip_nl(self):
        while self.peek()[0] == "nl":
            self.next()

    def body(self, closing: str | None):
        attrs, blocks = {}, []
        while True:
            self.skip_nl()
            kind, val, line = self.peek()
            if kind == "eof":
                if closing:
                    raise HCLError("unexpected end of file (missing '}')")
                return attrs, blocks
            if kind == "punct" and val == closing:
                self.next()
                return attrs, blocks
            if kind not in ("ident", "string"):
                raise HCLError(f"line {line}: unexpected {val!r}")
            self.next()
            name = val
            k2, v2, _ = self.peek()
            if k2 == "punct" and v2 == "=":
                self.next()
                attrs[name] = self.expr(stop={"nl"}, stop_punct={closing} if closing else set())
                continue
            labels = []
            while self.peek()[0] in ("string", "ident"):
                labels.append(self.next()[1])
            k3, v3, l3 = self.next()
            if not (k3 == "punct" and v3 == "{"):
                raise HCLError(f"line {l3}: expected '{{' after block {name!r}")
            sub_attrs, sub_blocks = self.body("}")
            blocks.append(Block(name, labels, sub_attrs, sub_blocks, line))

    def expr(self, stop, stop_punct):
        """Parse one expression; stops (without consuming) at a token kind in
        `stop` or a punct in `stop_punct` at bracket depth 0."""
        kind, val, _ = self.peek()
        if kind == "punct" and val == "{":
            start = self.i
            obj = self.obj()
            if obj is not None and self._at_end(stop, stop_punct):
                return obj
            self.i = start
        elif kind == "punct" and val == "[":
            start = self.i
            tup = self.tup()
            if tup is not None and self._at_end(stop, stop_punct):
                return tup
            self.i = start
        elif kind in ("string", "heredoc"):
            self.next()
            if self._at_end(stop, stop_punct):
                return val
            self.i -= 1
        elif kind == "ident" and val in ("true", "false", "null"):
            self.next()
            if self._at_end(stop, stop_punct):
                return {"true": True, "false": False, "null": None}[val]
            self.i -= 1
        return self.raw(stop, stop_punct)

    def _at_end(self, stop, stop_punct):
        kind, val, _ = self.peek()
        return kind in stop or kind == "eof" or (kind == "punct" and val in stop_punct)

    def raw(self, stop, stop_punct):
        parts, depth = [], []
        while True:
            kind, val, line = self.peek()
            if kind == "eof":
                if depth:
                    raise HCLError("unbalanced brackets")
                break
            if not depth and (kind in stop or (kind == "punct" and val in stop_punct)):
                break
            self.next()
            if kind == "punct" and val in _OPEN:
                depth.append(_OPEN[val])
            elif kind == "punct" and val in (")", "]", "}"):
                if not depth or depth[-1] != val:
                    raise HCLError(f"line {line}: unbalanced {val!r}")
                depth.pop()
            parts.append(f'"{val}"' if kind == "string" else val)
        return Raw(" ".join(p for p in parts if p != "\n").strip())

    def obj(self):
        """Object constructor { k = v, ... }. Returns dict, or None if this
        isn't a plain object (e.g. a for-expression)."""
        self.next()  # {
        out = {}
        while True:
            self.skip_nl()
            kind, val, _ = self.peek()
            if kind == "punct" and val == "}":
                self.next()
                return out
            if kind == "ident" and val == "for":
                return None
            if kind not in ("ident", "string"):
                return None
            self.next()
            k2, v2, _ = self.next()
            if not (k2 == "punct" and v2 in ("=", ":")):
                return None
            out[val] = self.expr(stop={"nl"}, stop_punct={",", "}"})
            if self.peek()[0] == "punct" and self.peek()[1] == ",":
                self.next()

    def tup(self):
        self.next()  # [
        out = []
        while True:
            self.skip_nl()
            kind, val, _ = self.peek()
            if kind == "punct" and val == "]":
                self.next()
                return out
            if kind == "ident" and val == "for":
                return None
            out.append(self.expr(stop={"nl"}, stop_punct={",", "]"}))
            self.skip_nl()
            if self.peek()[0] == "punct" and self.peek()[1] == ",":
                self.next()


def parse(src: str) -> Block:
    """Parse a whole file. Returns a synthetic root Block (type '')."""
    p = _Parser(tokenize(src))
    attrs, blocks = p.body(None)
    return Block("", [], attrs, blocks, 0)
