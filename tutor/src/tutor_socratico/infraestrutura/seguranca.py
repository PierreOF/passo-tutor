"""Password hashing and opaque browser session helpers."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import (
        InvalidHashError,
        VerificationError,
        VerifyMismatchError,
    )
except ImportError:  # Allows the stdlib-only local test environment to run.
    PasswordHasher = None  # type: ignore[assignment,misc]
    InvalidHashError = VerificationError = VerifyMismatchError = Exception  # type: ignore[assignment,misc]


def hash_password(password: str) -> str:
    if PasswordHasher is not None:
        return PasswordHasher().hash(password)
    if os.getenv("APP_ENV", "development").casefold() == "production":
        raise RuntimeError("Argon2id indisponível; instale argon2-cffi antes de iniciar em produção.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024)
    return "scrypt$" + salt.hex() + "$" + digest.hex()


def require_argon2id() -> None:
    if PasswordHasher is None:
        raise RuntimeError("Argon2id indisponÃ­vel; instale argon2-cffi antes de iniciar em produÃ§Ã£o.")


def verify_password(password: str, encoded: str) -> bool:
    if encoded.startswith("scrypt$"):
        try:
            _, salt_hex, digest_hex = encoded.split("$", 2)
            expected = bytes.fromhex(digest_hex)
            actual = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024)
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False
    if PasswordHasher is None:
        if os.getenv("APP_ENV", "development").casefold() == "production":
            raise RuntimeError("Argon2id indisponível; instale argon2-cffi antes de iniciar em produção.")
        raise RuntimeError("Instale argon2-cffi para verificar credenciais Argon2id.")
    try:
        return bool(PasswordHasher().verify(encoded, password))
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_secret(size: int = 32) -> str:
    return secrets.token_urlsafe(size)
