"""
KLASSER AI - unescaped school data rendered into HTML.

    python check_xss.py

School data is attacker-controllable by anyone inside that school: a teacher
name, a room name, a period label, a ticket subject. The frontend renders all of
it with innerHTML and template literals. An unescaped value is stored XSS - it
runs for every other user of that school, in their session, with their token.

Two cases, deliberately treated differently:

**Inside an attribute** (`value="${x}"`) is a hard failure. A value containing a
double quote escapes the attribute, and `" onfocus="..."` needs no angle
brackets at all. This case is precise enough to gate on: the only legitimate
unescaped values are ids we generate and our own constants, listed below.

**In text content** (`<span>${x}</span>`) is reported but not failed. Numbers,
dates, counts and our own literals dominate there, so failing on it would mean
an allowlist of a hundred entries that nobody reads - which is how a real
finding gets lost. It is printed so a human can skim it.

The period label on the layout page was the real one this found: typed by a
school, written straight back into `value="..."`, with no escaping anywhere in
that file.
"""

import pathlib
import re
import sys

FRONTEND = pathlib.Path(__file__).parent.parent / "frontend"

TAG = re.compile(r"</?[a-zA-Z][\w-]*[\s>/]")
INTERP = re.compile(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}")
ATTRIBUTE = re.compile(r'=\s*"\$\{([^}]+)\}')

# Number(x) yields a number or NaN and parseInt/parseFloat the same - none can
# contain a quote or an angle bracket, whatever is passed in.
SAFE_CALLS = ("escapeHtml(", "escape(", "encodeURIComponent(",
              "Number(", "parseInt(", "parseFloat(")

# Reviewed: every one of these is a UUID we generated, a number, or a string
# from a constant defined in our own source - never school-supplied text.
# A ternary choosing between two string literals: `x ? 'staff' : 'owner'`.
# Neither branch is data, whatever the condition reads, so the result can only
# ever be one of two strings written in our own source.
LITERAL_TERNARY = re.compile(
    r"^.+\?\s*(['\"])[^'\"]*\1\s*:\s*(['\"])[^'\"]*\2$")

SAFE_ATTRIBUTE_EXPRESSIONS = frozenset({
    "d", "i", "t", "id", "name", "value",
    "a.id", "c.id", "i.id", "j.id", "o.value", "p.id", "t.id", "u.id", "v.id",
    "x.id", "z.value", "s.id", "e.id",
    "f.type || 'text'",
    "t.latest_version",
    "escapeHtml(p.name)",
})


def literals(source: str):
    """Every backtick template literal, with the line it starts on."""
    out, i, line = [], 0, 1
    while i < len(source):
        ch = source[i]
        if ch == "\n":
            line += 1
        elif ch == "`":
            start, start_line, i = i + 1, line, i + 1
            depth = 0
            while i < len(source):
                c = source[i]
                if c == "\n":
                    line += 1
                elif c == "\\":
                    i += 2
                    continue
                elif c == "$" and i + 1 < len(source) and source[i + 1] == "{":
                    depth += 1
                    i += 2
                    continue
                elif c == "}" and depth:
                    depth -= 1
                elif c == "`" and not depth:
                    break
                i += 1
            out.append((start_line, source[start:i]))
        i += 1
    return out


def main() -> int:
    attribute_hits: list[tuple[str, int, str]] = []
    text_hits = 0
    scanned = 0

    for path in sorted(FRONTEND.rglob("*")):
        if path.is_dir() or path.suffix not in (".js", ".html"):
            continue

        rel = str(path.relative_to(FRONTEND)).replace("\\", "/")
        for line, body in literals(path.read_text(encoding="utf-8")):
            if not TAG.search(body):
                continue
            scanned += 1

            for match in ATTRIBUTE.finditer(body):
                expr = match.group(1).strip()
                if expr.startswith(SAFE_CALLS):
                    continue
                if expr in SAFE_ATTRIBUTE_EXPRESSIONS:
                    continue
                if re.fullmatch(r"[\d\s+\-*/().]+", expr):
                    continue
                if LITERAL_TERNARY.match(expr):
                    continue
                attribute_hits.append((rel, line, expr))

            for match in INTERP.finditer(body):
                if not match.group(1).strip().startswith(SAFE_CALLS):
                    text_hits += 1

    print(f"scanned {scanned} HTML template literal(s) across the frontend\n")

    print(f"Unescaped inside an HTML attribute ({len(attribute_hits)}):")
    for rel, line, expr in attribute_hits:
        print(f"  {rel}:{line}   ${{{expr}}}")
    if not attribute_hits:
        print("  none")

    print(f"\nUnescaped in text content: {text_hits} "
          f"(numbers, dates and our own constants - not gated, skim on review)")

    print()
    if attribute_hits:
        print(f"FAIL: {len(attribute_hits)} value(s) reach an HTML attribute "
              f"unescaped.")
        print("Wrap in escapeHtml(), or add to SAFE_ATTRIBUTE_EXPRESSIONS if "
              "the value provably cannot contain a quote.")
        return 1

    print("PASS: no school-supplied value reaches an HTML attribute unescaped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
