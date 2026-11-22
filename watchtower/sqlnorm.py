"""Python port of services/worker/src/sqlnorm.rs: literal-free statement text plus its XXH3-64 hash as
a decimal string, so SDK spans join agent DBM and the worker's wt.db.query_hash.
Spec: docs/product/instrumentation/sql-normalization.md; contract: testdata/sql_normalize_vectors.json."""
import re

import xxhash

MAX_LEN = 8192
_MAX_DOLLAR_TAG = 32
_PG = ("postgresql", "cockroachdb", "redshift")
# Unicode White_Space (Rust char::is_whitespace); str.isspace also takes \x1c-\x1f
_SPACE = frozenset("\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000")


def pg(system):
    """True when db.system.name uses the Postgres dialect."""
    return system in _PG


def _alnum(c):
    # Rust char::is_alphanumeric; shortcut: str.isalnum also takes a few non-N* numerics (e.g. superscripts)
    return c.isalnum()


def _word(c):
    return c.isalnum() or c == "_" or c == "$"


def _digit(c):
    return "0" <= c <= "9"


def _cap(s):
    b = s.encode("utf-8", "surrogatepass")
    if len(b) <= MAX_LEN:
        return s
    return b[:MAX_LEN].decode("utf-8", "ignore")  # drops a cut trailing character


def _dollar_quoted(cs, i):
    window = cs[i + 1:i + 2 + _MAX_DOLLAR_TAG]
    k = window.find("$")
    if k < 0:
        return None
    tag = cs[i:i + k + 2]
    if not all(_alnum(c) or c == "_" for c in tag[1:k + 1]) or _digit(tag[1]):
        return None
    j = cs.find(tag, i + len(tag))
    return len(cs) if j < 0 else j + len(tag)


def _token(cs, i, is_pg, out):
    c = cs[i]
    n = len(cs)
    if c == "'":
        i += 1
        while i < n:
            if cs[i] == "'":
                if cs[i + 1:i + 2] != "'":
                    break
                i += 1
            i += 1
        out.append("?")
        return n if i < n and cs[i - 1] == "\\" else i + 1  # ambiguous MySQL escape: mask the rest
    if c in "\"`":
        close = cs.find(c, i + 1)
        close = n if close < 0 else close
        body = cs[i + 1:close]
        ident = (c == '"' and is_pg and close < n and 1 <= len(body) <= 64
                 and all(_alnum(b) or b in "_$" for b in body))
        out.append(cs[i:close + 1] if ident else "?")
        return close + 1
    if c == "$":
        if i + 1 < n and _digit(cs[i + 1]):
            i += 1
            while i < n and _digit(cs[i]):
                i += 1
            out.append("?")
            return i
        end = _dollar_quoted(cs, i)
        if end is not None:
            out.append("?")
            return end
    if _digit(c):
        while i < n and (_word(cs[i]) or cs[i] == "."):
            i += 1
        out.append("?")
        return i
    if _word(c):
        j = i
        while j < n and _word(cs[j]):
            j += 1
        out.append(cs[i:j])
        return j
    out.append(c)
    return i + 1


def _in_list(b, i):
    def skip(i):
        while i < len(b) and b[i] == 0x20:
            i += 1
        return i

    i = skip(i)
    opn = i
    if i >= len(b) or b[i] != 0x28:  # (
        return None
    i += 1
    while True:
        i = skip(i)
        if i >= len(b) or b[i] != 0x3F:  # ?
            return None
        i = skip(i + 1)
        if i >= len(b):
            return None
        if b[i] == 0x2C:  # ,
            i += 1
        elif b[i] == 0x29:  # )
            return opn, i + 1
        else:
            return None


def _collapse_in(s):
    """`in (?, ?)` -> `in (?+)` on the UTF-8 bytes; the word boundary reads the previous byte as Latin-1."""
    b = s.encode("utf-8", "surrogatepass")
    out = bytearray()
    i = 0
    while i < len(b):
        at_word = i == 0 or not _word(chr(b[i - 1]))
        if at_word and len(b) - i >= 2 and b[i] | 0x20 == 0x69 and b[i + 1] | 0x20 == 0x6E:
            m = _in_list(b, i + 2)
            if m:
                out += b[i:m[0]] + b"(?+)"
                i = m[1]
                continue
        out.append(b[i])
        i += 1
    return out.decode("utf-8", "surrogatepass")


def normalize(stmt, is_pg=False):
    """The normalized statement; `is_pg` selects the Postgres dialect."""
    cs = _cap(stmt)
    n = len(cs)
    out = []
    space = False
    i = 0
    while i < n:
        c = cs[i]
        nxt = cs[i + 1:i + 2]
        if c in _SPACE:
            space = True
            i += 1
        elif c == "/" and nxt == "*":
            depth, i = 1, i + 2
            while i < n and depth > 0:
                two = cs[i:i + 2]
                if two == "/*":
                    depth, i = depth + 1, i + 2
                elif two == "*/":
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            space = True
        elif (c == "-" and nxt == "-") or (c == "#" and not is_pg):
            end = cs.find("\n", i)
            i = n if end < 0 else end
            space = True
        else:
            if space and out:
                out.append(" ")
            space = False
            i = _token(cs, i, is_pg, out)
    return _collapse_in("".join(out))


def query_hash(normalized):
    """XXH3-64 (seed 0) of the normalized text as an unsigned decimal string."""
    return str(xxhash.xxh3_64_intdigest(normalized.encode("utf-8", "surrogatepass")))


_OP = re.compile(r"^\s*(?:with\b[\s\S]*?\)\s*)?([a-z]+)", re.I | re.A)
_TARGET = re.compile(r'\b(?:from|into|update|join|table)\s+((?:"[^"]+"|[\w$]+)(?:\.(?:"[^"]+"|[\w$]+))?)', re.I | re.A)


def summary(normalized):
    """(OPERATION, first table) of normalized text, like the Node SDK's querySummary; either may be ""."""
    op = _OP.match(normalized)
    t = _TARGET.search(normalized)
    return (op.group(1).upper() if op else ""), (t.group(1).replace('"', "") if t else "")


def attributes(stmt, with_text=False, system="sqlite"):
    """db.query.hash, db.query.summary, db.operation.name, db.collection.name and, when with_text,
    the sanitized db.query.text."""
    if not stmt:
        return {}
    n = normalize(stmt, pg(system))
    a = {"db.query.hash": query_hash(n)}
    op, table = summary(n)
    if op:
        a["db.operation.name"] = op
        a["db.query.summary"] = f"{op} {table}" if table else op
        if table:
            a["db.collection.name"] = table
    if with_text:
        a["db.query.text"] = n
    return a
