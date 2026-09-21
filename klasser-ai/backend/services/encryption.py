"""
Symmetric encryption for AI provider keys held in the database.

Provider keys are stored encrypted in `api_keys.encrypted_key` and decrypted
into memory at startup. They are never written to disk in plaintext, never
logged, and never returned by any endpoint - the dev portal only ever sees a
masked hint.

ENCRYPTION_KEY must never change once keys are stored: rotating it makes every
stored key undecryptable. To rotate it deliberately, use reencrypt_all().
"""

import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

import keys

log = logging.getLogger("klasser.encryption")

_fernet: Optional[Fernet] = None


class EncryptionUnavailable(RuntimeError):
    """ENCRYPTION_KEY is missing or malformed."""


def fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        if not keys.ENCRYPTION_KEY:
            raise EncryptionUnavailable(
                "ENCRYPTION_KEY is not set. Generate one with: python -c "
                '"from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
        try:
            _fernet = Fernet(keys.ENCRYPTION_KEY.encode())
        except (ValueError, TypeError) as exc:
            raise EncryptionUnavailable(
                "ENCRYPTION_KEY is not a valid Fernet key (32 url-safe base64 bytes)"
            ) from exc
    return _fernet


def encrypt_key(plain_text: str) -> str:
    return fernet().encrypt(plain_text.encode()).decode()


def decrypt_key(encrypted: str) -> str:
    try:
        return fernet().decrypt(encrypted.encode()).decode()
    except InvalidToken as exc:
        # Almost always means ENCRYPTION_KEY changed since the row was written.
        raise EncryptionUnavailable(
            "Stored key could not be decrypted. ENCRYPTION_KEY does not match "
            "the one used to encrypt it."
        ) from exc


def mask(plain_text: str) -> str:
    """
    A safe hint for the dev portal: 'sk-abc...wxyz'.

    Short strings are fully masked rather than partly revealed.
    """
    if len(plain_text) < 12:
        return "*" * len(plain_text)
    return f"{plain_text[:6]}...{plain_text[-4:]}"


def is_configured() -> bool:
    try:
        fernet()
        return True
    except EncryptionUnavailable:
        return False
