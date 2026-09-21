"""
Simulated card terminal (BILLING.md).

No real payment is ever processed. Whatever the user types is turned into a
realistic-looking card record, deterministically: the same input always yields
the same card, so a demo can be repeated and a test can assert on the result.

Everything here is fake by construction. The generated numbers are Luhn-valid
so they look right in a UI and format correctly, and are drawn from the ranges
card networks reserve for testing - they cannot correspond to a real card.
"""

import hashlib
import random
import string
from datetime import datetime, timezone
from typing import Optional

# Test-only prefixes. 4111/5500/3400 are the standard test ranges; no issuer
# allocates real cards from them.
BRANDS = [
    ("Visa", "4111", 16, 3),
    ("Mastercard", "5500", 16, 3),
    ("Amex", "3400", 15, 4),
]


def luhn_check_digit(partial: str) -> int:
    """The digit that makes `partial` + digit pass the Luhn checksum."""
    total = 0
    for i, char in enumerate(reversed(partial)):
        digit = int(char)
        # Double every second digit counting from the right of the final number,
        # which is every digit at even index in this reversed partial.
        if i % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return (10 - (total % 10)) % 10


def luhn_valid(number: str) -> bool:
    total = 0
    for i, char in enumerate(reversed(number)):
        digit = int(char)
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def process_fake_card(raw_input: str) -> dict:
    """
    Turn arbitrary typed input into a plausible card record.

    The input seeds the generator, so the same keystrokes always produce the
    same card - which makes a demo reproducible and lets tests assert exact
    values.
    """
    seed = hashlib.sha256((raw_input or "").encode()).hexdigest()
    rng = random.Random(seed)

    brand, prefix, length, cvv_length = rng.choice(BRANDS)

    body = prefix + "".join(str(rng.randint(0, 9))
                            for _ in range(length - len(prefix) - 1))
    number = body + str(luhn_check_digit(body))

    now = datetime.now(timezone.utc)
    exp_year = now.year + rng.randint(1, 4)
    exp_month = rng.randint(1, 12)

    def token(prefix_: str, size: int = 24) -> str:
        return prefix_ + "".join(
            rng.choices(string.ascii_letters + string.digits, k=size))

    return {
        "payment_method_id": token("pm_"),
        "last_four": number[-4:],
        "brand": brand,
        "expires": f"{exp_month:02d}/{str(exp_year)[-2:]}",
        "cvv_length": cvv_length,
        "masked": f"{'*' * (length - 4)}{number[-4:]}",
        # Kept for tests and the dev portal; never shown to a school.
        "_number": number,
    }


def charge(raw_seed: str, amount_cents: int,
           success_rate: float = 1.0) -> dict:
    """
    Simulate charging a saved card.

    `success_rate` comes from the `fake_terminal_success_rate` setting, which
    the dev portal uses to exercise the declined-payment path: 1.0 always
    succeeds, 0.0 always declines, 0.8 declines about one time in five.
    """
    rng = random.Random(
        hashlib.sha256(f"{raw_seed}:{amount_cents}".encode()).hexdigest())

    succeeded = rng.random() < max(0.0, min(1.0, success_rate))

    result = {
        "success": succeeded,
        "amount_cents": amount_cents,
        "charge_id": "ch_" + "".join(
            rng.choices(string.ascii_letters + string.digits, k=24)),
    }
    if not succeeded:
        result["failure_reason"] = rng.choice([
            "card_declined", "insufficient_funds", "expired_card",
            "processing_error",
        ])
    return result


def format_card_number(number: str, brand: Optional[str] = None) -> str:
    """Group digits the way a terminal would: 4-6-5 for Amex, 4-4-4-4 else."""
    digits = "".join(c for c in number if c.isdigit())
    if brand == "Amex" or len(digits) == 15:
        return f"{digits[:4]} {digits[4:10]} {digits[10:]}".strip()
    return " ".join(digits[i:i + 4] for i in range(0, len(digits), 4)).strip()
