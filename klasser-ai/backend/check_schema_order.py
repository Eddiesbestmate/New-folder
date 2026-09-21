"""
Verify 001_schema.sql is in strict dependency order: every REFERENCES target is
created before the table that references it. Self-references are allowed.
Run after any schema edit.
"""

import re
import sys
from pathlib import Path

SQL = Path(__file__).parent / "migrations" / "001_schema.sql"

text = SQL.read_text(encoding="utf-8")
# Strip line comments so commented-out FKs are not counted.
text = re.sub(r"--[^\n]*", "", text)

# Split into CREATE TABLE blocks, keeping order of appearance.
blocks = re.findall(
    r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(\w+)\s*\((.*?)\n\);",
    text,
    re.S | re.I,
)

created: list[str] = []
problems: list[str] = []

for name, body in blocks:
    targets = re.findall(r"REFERENCES\s+([\w.]+)\s*\(", body, re.I)
    for target in targets:
        if target.startswith("auth."):
            continue  # Supabase-managed, always present
        if target == name:
            continue  # self-reference
        if target not in created:
            problems.append(f"{name} references {target}, which is created later")
    created.append(name)

print(f"tables: {len(created)}")
print(f"foreign keys checked: "
      f"{sum(len(re.findall(r'REFERENCES', b, re.I)) for _, b in blocks)}")

dupes = {n for n in created if created.count(n) > 1}
if dupes:
    problems.append(f"duplicate table definitions: {sorted(dupes)}")

if problems:
    print("\nPROBLEMS:")
    for p in problems:
        print(f"  - {p}")
    sys.exit(1)

print("dependency order: ok - every foreign key target is created first")
