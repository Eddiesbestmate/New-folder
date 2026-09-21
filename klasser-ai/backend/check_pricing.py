"""
KLASSER AI - does the marketing site quote the prices we would actually charge?

    python check_pricing.py

The pricing page is hand-written HTML; credit_packages is what the billing code
bills from, and the dev portal can change it at runtime. Nothing keeps the two
in step, so a package edited in the portal leaves the public page quoting a
price no school will ever be charged - which is the worst kind of wrong, because
it is invisible until someone complains about their invoice.

Exit 0 when every active package and the PAYG rate appear on the page with the
right credits and per-credit rate.
"""

import asyncio
import pathlib
import sys

from models import database as db

PAGE = pathlib.Path(__file__).parent.parent / "frontend" / "index.html"


async def main() -> int:
    if not PAGE.exists():
        print(f"No pricing page at {PAGE}")
        return 1

    await db.connect()
    if db.pool() is None:
        print("DATABASE_URL is not set.")
        return 1

    packages = await db.fetch("""
        SELECT name, display_name, credits, price_per_credit, total_price_cents,
               is_active
        FROM credit_packages ORDER BY display_order
    """)
    payg_cents = await db.fetchval(
        "SELECT value FROM settings WHERE key = 'credit_payg_rate_cents'")
    await db.disconnect()

    html = PAGE.read_text(encoding="utf-8")
    problems: list[str] = []

    print("Credit packages versus the pricing page\n")

    for row in packages:
        dollars = f"${(row['total_price_cents'] or 0) / 100:,.0f}"
        rate = f"${float(row['price_per_credit']):.2f}"
        credits = f"{row['credits']:,}"
        shown = dollars in html

        state = "on the page" if shown else "NOT SHOWN"
        if not row["is_active"]:
            state += "  (inactive, so absence is fine)"

        print(f"  {row['display_name']:<12} {credits:>6} cr  {dollars:>8}  "
              f"{rate}/cr   {state}")

        if shown:
            # Quoting the price but not the credits beside it is the same
            # problem in a subtler form.
            if credits not in html and str(row["credits"]) not in html:
                problems.append(
                    f"{row['display_name']}: price shown but {credits} credits "
                    "is not")
            if rate not in html:
                problems.append(
                    f"{row['display_name']}: price shown but the {rate} "
                    "per-credit rate is not")
        elif row["is_active"]:
            problems.append(
                f"{row['display_name']} can be bought in the app but is not on "
                "the pricing page")

    payg = f"${int(payg_cents or 0) / 100:.2f}"
    payg_shown = payg in html
    print(f"\n  {'Pay as you go':<12} {'':>6}     {payg:>8}  per credit   "
          f"{'on the page' if payg_shown else 'NOT SHOWN'}")
    if not payg_shown:
        problems.append(f"the PAYG rate {payg} is not on the pricing page")

    print()
    if problems:
        for problem in problems:
            print(f"  MISMATCH: {problem}")
        print(f"\nFAIL: {len(problems)} pricing mismatch(es).")
        return 1

    print("PASS: every advertised price matches the database.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
