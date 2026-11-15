"""API key primitives: format, entropy, hashing, parsing, expiry math, usability, permissions."""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from app.services.auth import apikeys
from app.services.auth.accounts import InvalidError
from app.services.auth.permissions import KEY_FORBIDDEN, Permission

NOW = datetime(2026, 1, 1, 12, 0, 0)


def _key(**kw: Any) -> Any:
    base: dict[str, Any] = {
        "revoked_at": None,
        "expires_at": None,
        "owner": SimpleNamespace(is_active=True),
        "role": SimpleNamespace(permissions=[]),
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_generated_key_format_and_parse_roundtrip() -> None:
    for _ in range(200):
        prefix, secret = apikeys.generate()
        full = apikeys.format_key(prefix, secret)
        assert full.startswith("rag_")
        assert len(prefix) == apikeys.PREFIX_LEN
        assert prefix.isalnum() and prefix == prefix.lower()
        assert apikeys.parse(full) == (prefix, secret)  # secrets may contain '_' and '-'


def test_secret_has_256_bits_and_is_not_repeated() -> None:
    secrets_seen = {apikeys.generate()[1] for _ in range(500)}
    assert len(secrets_seen) == 500
    sample = next(iter(secrets_seen))
    assert len(sample) >= 43  # token_urlsafe(32) -> 43 chars of base64url
    # crude entropy sanity: per-character Shannon entropy over many secrets is near log2(64)=6 bits
    counts = Counter("".join(secrets_seen))
    total = sum(counts.values())
    entropy = -sum(c / total * math.log2(c / total) for c in counts.values())
    assert entropy > 5.7


def test_prefixes_are_random_not_sequential() -> None:
    prefixes = {apikeys.generate()[0] for _ in range(500)}
    assert len(prefixes) > 495  # 36^8 space: collisions are astronomically unlikely


def test_hash_is_sha256_hex_of_the_secret_only() -> None:
    assert apikeys.hash_secret("abc") == hashlib.sha256(b"abc").hexdigest()
    assert len(apikeys.hash_secret("x")) == 64
    assert apikeys.hash_secret("a") != apikeys.hash_secret("b")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "rag_",
        "rag_abcd1234",  # no secret
        "rag_abcd1234_",  # empty secret
        "rag_abcd12_secretsecret",  # prefix too short
        "rag_ABCD1234_secret",  # uppercase prefix
        "rag_abcd-234_secret",  # bad prefix char
        "rag_abcd1234secret",  # missing separator
        "xyz_abcd1234_secret",
        "eyJhbGciOi.JIUzI1NiJ9.sig",
    ],
)
def test_parse_rejects_malformed(raw: str) -> None:
    assert apikeys.parse(raw) is None


def test_parse_tolerates_surrounding_whitespace() -> None:
    assert apikeys.parse("  rag_abcd1234_s3cret \n") == ("abcd1234", "s3cret")


def test_expiry_math() -> None:
    assert apikeys.expiry_for(None, NOW) is None
    assert apikeys.expiry_for(1, NOW) == NOW + timedelta(days=1)
    assert apikeys.expiry_for(90, NOW) == NOW + timedelta(days=90)
    for bad in (0, -1, apikeys.MAX_EXPIRY_DAYS + 1):
        with pytest.raises(InvalidError):
            apikeys.expiry_for(bad, NOW)


def test_usability_checks_revoked_expiry_and_owner() -> None:
    assert apikeys.is_usable(_key(), NOW)
    assert not apikeys.is_usable(_key(revoked_at=NOW - timedelta(days=1)), NOW)
    assert apikeys.is_usable(_key(expires_at=NOW + timedelta(seconds=1)), NOW)
    assert not apikeys.is_usable(_key(expires_at=NOW), NOW)  # expires AT the instant
    assert not apikeys.is_usable(_key(expires_at=NOW - timedelta(seconds=1)), NOW)
    assert not apikeys.is_usable(_key(owner=SimpleNamespace(is_active=False)), NOW)


def test_permissions_come_from_role_minus_management() -> None:
    perms = [SimpleNamespace(permission=p.value) for p in Permission]
    got = apikeys.permissions_of(_key(role=SimpleNamespace(permissions=perms)))
    assert got == frozenset(p.value for p in Permission) - KEY_FORBIDDEN
    assert not (got & KEY_FORBIDDEN)
    assert {"users:manage", "roles:manage", "apikeys:manage", "settings:manage"} == set(KEY_FORBIDDEN)


def test_warning_only_for_admin_role() -> None:
    assert apikeys.warnings_for(SimpleNamespace(name="admin"))  # type: ignore[arg-type]  # duck-typed Role
    assert apikeys.warnings_for(SimpleNamespace(name="viewer")) == []  # type: ignore[arg-type]  # duck-typed Role
