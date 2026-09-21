"""
Static audit of the codebase and its tests.

Two kinds of finding:

  Test weakness  - assertions that can pass without exercising their subject.
                   Two of these were found by hand in the billing suite: a
                   status-code check that accepted 402 *or* 422 and was
                   satisfied by the wrong refusal, and a concurrency test that
                   skipped itself. Both reported PASS for their whole life.

  Code risk      - endpoints without an auth dependency, SQL built by string
                   formatting, secrets reachable from the frontend, settings
                   that nothing reads.

Everything here is heuristic. A finding is a question to answer, not a defect.

    python audit.py [--tests] [--code] [--verbose]
"""

import argparse
import ast
import re
import sys
from collections import defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
FRONTEND = BACKEND.parent / "frontend"

findings: list[tuple[str, str, str, str]] = []  # severity, area, location, message


def note(severity: str, area: str, location: str, message: str) -> None:
    findings.append((severity, area, location, message))


# --- Test weaknesses ---------------------------------------------------------

def audit_tests() -> None:
    for path in sorted(BACKEND.glob("test_*.py")):
        # utf-8-sig so a stray BOM is stripped rather than reported as a
        # syntax error. Python itself tolerates one; only naive readers trip.
        source = path.read_text(encoding="utf-8-sig")
        lines = source.splitlines()

        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            note("HIGH", "tests", path.name, f"does not parse: {exc}")
            continue

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "check"):
                continue
            if not node.args:
                continue

            condition = node.args[0]
            label = ""
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                label = str(node.args[1].value)
            where = f"{path.name}:{node.lineno}"

            # check(True, ...) can never fail.
            if isinstance(condition, ast.Constant) and condition.value is True:
                note("HIGH", "tests", where,
                     f"unconditional pass: check(True, {label!r})")

            # `x in (a, b)` on a status code accepts more than one outcome.
            if isinstance(condition, ast.Compare) and any(
                    isinstance(op, ast.In) for op in condition.ops):
                comparator = condition.comparators[0]
                if isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                    values = [e.value for e in comparator.elts
                              if isinstance(e, ast.Constant)]
                    if len(values) > 1 and all(
                            isinstance(v, int) and 100 <= v < 600 for v in values):
                        note("HIGH", "tests", where,
                             f"accepts several status codes {values}: {label!r} "
                             "can pass on the wrong one")

            # `a or b` passes if either half holds.
            if isinstance(condition, ast.BoolOp) and isinstance(condition.op, ast.Or):
                note("MEDIUM", "tests", where,
                     f"disjunctive assertion: {label!r} passes if either side does")

            # count >= 0 is true of every count.
            if isinstance(condition, ast.Compare) and len(condition.ops) == 1:
                op = condition.ops[0]
                right = condition.comparators[0]
                if (isinstance(op, ast.GtE) and isinstance(right, ast.Constant)
                        and right.value == 0):
                    note("HIGH", "tests", where,
                         f"always true: {label!r} asserts a count is >= 0")

        # A check() inside an else branch whose if-branch has no check is a
        # silent skip.
        for i, line in enumerate(lines):
            if re.match(r"\s*else:\s*$", line):
                following = "\n".join(lines[i + 1:i + 4])
                if "check(True" in following or "skipped" in following.lower():
                    note("HIGH", "tests", f"{path.name}:{i + 1}",
                         "else branch reports a pass - the real case may be skipped")

        # `except ImportError: ...` guarding an optional dependency is a
        # deliberate pattern, not a swallowed failure. Anything else is worth a
        # look.
        for match in re.finditer(
                r"except\s+(?!ImportError)([^\n]*):\s*\n\s*(pass|continue)\b",
                source):
            line = source[:match.start()].count("\n") + 1
            note("MEDIUM", "tests", f"{path.name}:{line}",
                 f"exception swallowed ({match.group(1).strip()}) - a failure "
                 "here would not be reported")


# --- Code risks --------------------------------------------------------------

# Type aliases count too: routers/dev.py declares
# `DevUser = Annotated[dict, Depends(require_dev_role)]` and every endpoint
# takes `user: DevUser`, which is a real guard even though the signature names
# no dependency directly.
AUTH_MARKERS = ("CurrentUser", "DevUser", "require_owner", "require_dev_role",
                "get_current_user")

# Endpoints that are public by design.
PUBLIC_ENDPOINTS = {
    ("auth", "signup"), ("auth", "timezones"),
    ("allocation", "progress_stream"),   # verifies a token from the query string
    # Returns two support addresses and a response time, nothing school-specific.
    # Public on purpose: a user who is locked out or cannot log in is exactly
    # the one who needs it.
    ("support", "contact"),
    # The invite flow, before the invitee has any session at all. The 32-byte
    # token in the link is the credential; both endpoints refuse an unknown,
    # expired or already-used token identically, so neither can be used to
    # discover which addresses have been invited.
    ("users", "look_up_invite"), ("users", "accept"),
}


def audit_routers() -> None:
    for path in sorted((BACKEND / "routers").glob("*.py")):
        if path.name == "__init__.py":
            continue
        source = path.read_text(encoding="utf-8-sig")
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            note("HIGH", "routers", path.name, f"does not parse: {exc}")
            continue

        module = path.stem
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue

            routes = [d for d in node.decorator_list
                      if isinstance(d, ast.Call)
                      and isinstance(d.func, ast.Attribute)
                      and d.func.attr in ("get", "post", "put", "patch", "delete")]
            if not routes:
                continue

            signature = ast.unparse(node.args)
            if not any(marker in signature for marker in AUTH_MARKERS):
                if (module, node.name) in PUBLIC_ENDPOINTS:
                    continue
                note("HIGH", "routers", f"{path.name}:{node.lineno}",
                     f"{node.name}() has no auth dependency")


SQL_METHODS = {"execute", "executemany", "fetch", "fetchrow", "fetchval"}

# What gets interpolated. A name on this list is a table or column identifier
# that comes from a literal, a fixed dict, or an allowlisted constant - checked
# by reading every call site. A value can never be interpolated: those use $n
# placeholders. Anything interpolating a name NOT on this list is new and
# unreviewed, and the audit will say so.
REVIEWED_SQL_IDENTIFIERS = frozenset({
    "table",      # always from a literal or an allowlist
    "column",     # from a literal dict in block_if_referenced()
    "where",      # assembled in code; every value inside is a $n placeholder
    "settings_join",  # a constant SQL fragment in test_pipeline
    # services/editing.py. Both come from AI output, so both are checked against
    # a fixed allowlist before reaching a statement: `field` must be in
    # EDITABLE_FIELDS, and FIELD_TABLES is a literal dict keyed by it. The value
    # written is always a $n placeholder.
    "field",
    "FIELD_TABLES",
    # routers/support.py. A module-level constant listing the columns a school
    # may see - the point of it is that internal_notes is not on it, so the
    # school-facing queries cannot start returning it by accident.
    "SCHOOL_COLUMNS",
})

def interpolated_names(node: ast.JoinedStr) -> set[str]:
    """The names substituted into an f-string, and nothing else."""
    names: set[str] = set()
    for part in node.values:
        if not isinstance(part, ast.FormattedValue):
            continue
        for sub in ast.walk(part.value):
            if isinstance(sub, ast.Name):
                names.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                names.add(sub.attr)
    return names


def audit_sql() -> None:
    """
    Find SQL built by string interpolation.

    Works on the parsed f-string rather than a window of surrounding text: a
    regex over nearby characters picks up unrelated f-strings in the same
    function and reports names that never touch SQL at all.
    """
    for path in sorted(BACKEND.rglob("*.py")):
        if "venv" in path.parts or path.name == "audit.py":
            continue
        source = path.read_text(encoding="utf-8-sig")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in SQL_METHODS
                    and node.args
                    and isinstance(node.args[0], ast.JoinedStr)):
                continue

            unreviewed = interpolated_names(node.args[0]) - REVIEWED_SQL_IDENTIFIERS
            if not unreviewed:
                continue

            note("MEDIUM", "sql", f"{path.relative_to(BACKEND)}:{node.lineno}",
                 "SQL interpolates "
                 + ", ".join(sorted(f"{{{n}}}" for n in unreviewed))
                 + " - if these are values, use $n placeholders; if they are "
                   "identifiers, add them to REVIEWED_SQL_IDENTIFIERS in audit.py")


SECRET_PATTERNS = [
    (re.compile(r"service_role", re.I), "service role key"),
    (re.compile(r"SUPABASE_SERVICE_KEY"), "service key name"),
    (re.compile(r"\bsk-[A-Za-z0-9]{12,}"), "API key literal"),
    (re.compile(r"postgresql://[^\s\"']*:[^\s\"'@]+@"), "database URL with password"),
]


def audit_frontend_secrets() -> None:
    if not FRONTEND.exists():
        return
    for path in sorted(FRONTEND.rglob("*")):
        if path.is_dir() or path.suffix not in (".js", ".html", ".css"):
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        for pattern, what in SECRET_PATTERNS:
            if pattern.search(source):
                note("CRITICAL", "frontend", str(path.relative_to(FRONTEND)),
                     f"possible {what} in a file served to browsers")


# Settings that exist for a phase not yet built. Each one is a promise: the
# control is visible in the dev portal and currently does nothing, so it must
# be wired up when that phase lands. Anything unused and NOT listed here is an
# unfulfilled promise nobody is tracking - which is how
# duty_default_max_per_cycle and the two quorum settings sat inert while being
# documented as working.
PENDING_SETTINGS = {
    "credit_edit_session": "Phase 11 - AI-assisted editing",
    "invoice_sent_day": "Phase 10 - monthly invoice job",
    "invoice_due_day": "Phase 10 - monthly invoice job",
    "invoice_block_days": "Phase 10 - invoice enforcement job",
    "invoice_revoke_days": "Phase 10 - invoice enforcement job",
    "invoice_interest_rate": "Phase 10 - interest accrual job",
    "payg_interest_rate": "Phase 10 - PAYG failure handling",
}


def audit_settings_usage() -> None:
    """
    Settings written by migrations that no code reads.

    Later migrations can delete a setting an earlier one added, so declarations
    are tracked in file order and removals honoured - otherwise a superseded
    setting is reported forever.
    """
    declared: set[str] = set()
    for path in sorted((BACKEND / "migrations").glob("*.sql")):
        source = path.read_text(encoding="utf-8-sig")
        for match in re.finditer(r"^\('([a-z0-9_]+)',", source, re.M):
            declared.add(match.group(1))
        for match in re.finditer(
                r"DELETE\s+FROM\s+settings\s+WHERE\s+key\s*=\s*'([a-z0-9_]+)'",
                source, re.I):
            declared.discard(match.group(1))

    used: set[str] = set()
    for path in sorted(BACKEND.rglob("*.py")):
        if "venv" in path.parts or path.name == "audit.py":
            continue
        source = path.read_text(encoding="utf-8-sig")
        for name in declared:
            if f"'{name}'" in source or f'"{name}"' in source:
                used.add(name)

    for name in sorted(declared - used):
        reason = PENDING_SETTINGS.get(name)
        if reason:
            note("LOW", "settings", name, f"not wired up yet: {reason}")
        else:
            note("HIGH", "settings", name,
                 "exists but nothing reads it - changing it in the dev portal "
                 "would have no effect. Wire it up, delete it, or add it to "
                 "PENDING_SETTINGS with the phase that will use it.")


def audit_platform_strftime() -> None:
    """
    strftime directives that only exist on one platform.

    `%-d` and friends are glibc extensions: they work on Linux and raise
    ValueError("Invalid format string") on Windows. Code using them runs on the
    server and crashes on a developer's machine, or the reverse - which is how
    this was found, when the deactivation email broke a passing test suite.
    Use config.long_date() instead.
    """
    pattern = re.compile(r"%-[dmHIMSjyU]")
    for path in sorted(BACKEND.rglob("*.py")):
        if "venv" in path.parts or path.name == "audit.py":
            continue
        for number, line in enumerate(
                path.read_text(encoding="utf-8-sig").splitlines(), 1):
            if pattern.search(line):
                note("HIGH", "portability",
                     f"{path.relative_to(BACKEND)}:{number}",
                     "strftime directive that does not exist on Windows - "
                     "use config.long_date()")


def audit_todos() -> None:
    marker = re.compile(r"#\s*(TODO|FIXME|XXX|HACK)\b[:\s]*(.*)")
    for path in sorted(BACKEND.rglob("*.py")):
        if "venv" in path.parts or path.name == "audit.py":
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            match = marker.search(line)
            if match:
                note("LOW", "todo", f"{path.relative_to(BACKEND)}:{i}",
                     f"{match.group(1)}: {match.group(2).strip()[:70]}")


# --- Report -------------------------------------------------------------------

ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def report(verbose: bool) -> int:
    if not findings:
        print("No findings.")
        return 0

    by_severity: dict[str, list] = defaultdict(list)
    for severity, area, location, message in findings:
        by_severity[severity].append((area, location, message))

    for severity in sorted(by_severity, key=lambda s: ORDER.get(s, 9)):
        items = by_severity[severity]
        print(f"\n{severity} ({len(items)})")
        print("-" * 76)
        shown = items if verbose else items[:25]
        for area, location, message in shown:
            print(f"  [{area}] {location}")
            print(f"      {message}")
        if len(items) > len(shown):
            print(f"  ... and {len(items) - len(shown)} more (use --verbose)")

    counts = {s: len(v) for s, v in by_severity.items()}
    print(f"\n{sum(counts.values())} finding(s): "
          + ", ".join(f"{n} {s.lower()}" for s, n in
                      sorted(counts.items(), key=lambda kv: ORDER.get(kv[0], 9))))

    # Only the serious ones fail the run.
    return 1 if counts.get("CRITICAL") or counts.get("HIGH") else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tests", action="store_true")
    parser.add_argument("--code", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    everything = not (args.tests or args.code)

    if everything or args.tests:
        audit_tests()
    if everything or args.code:
        audit_routers()
        audit_sql()
        audit_frontend_secrets()
        audit_settings_usage()
        audit_platform_strftime()
    audit_todos()

    return report(args.verbose)


if __name__ == "__main__":
    sys.exit(main())
