"""Password hashing, JWT tokens and the permission catalogue — pure logic, no services."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app.services.auth import passwords, tokens
from app.services.auth.permissions import BUILTIN_ROLES, Permission, is_valid_permission

SECRET = "x" * 40


def test_password_roundtrip_and_wrong_password() -> None:
    stored = passwords.hash_password("correct horse battery")
    assert passwords.verify_password("correct horse battery", stored)
    assert not passwords.verify_password("correct horse batterz", stored)
    assert "correct horse" not in stored


def test_same_password_hashes_differently_each_time() -> None:
    assert passwords.hash_password("same-password") != passwords.hash_password("same-password")


def test_short_password_rejected() -> None:
    with pytest.raises(passwords.WeakPasswordError):
        passwords.hash_password("short")


@pytest.mark.parametrize("stored", ["", "garbage", "scrypt$1$2", "bcrypt$a$b$c$d$e", "scrypt$x$y$z$@@$@@"])
def test_malformed_hash_verifies_false_instead_of_raising(stored: str) -> None:
    assert passwords.verify_password("whatever-pass", stored) is False


def test_dummy_hash_never_matches() -> None:
    assert not passwords.verify_password("anything-at-all", passwords.DUMMY_HASH)


def test_token_roundtrip() -> None:
    uid = uuid.uuid4()
    tok = tokens.issue(uid, "access", secret=SECRET, lifetime=timedelta(minutes=5))
    assert tokens.verify(tok, "access", secret=SECRET) == uid


def test_refresh_token_is_not_accepted_as_access_and_vice_versa() -> None:
    uid = uuid.uuid4()
    refresh = tokens.issue(uid, "refresh", secret=SECRET, lifetime=timedelta(days=1))
    access = tokens.issue(uid, "access", secret=SECRET, lifetime=timedelta(minutes=5))
    with pytest.raises(tokens.TokenError):
        tokens.verify(refresh, "access", secret=SECRET)
    with pytest.raises(tokens.TokenError):
        tokens.verify(access, "refresh", secret=SECRET)


def test_expired_token_rejected() -> None:
    past = datetime.now(UTC) - timedelta(hours=2)
    tok = tokens.issue(
        uuid.uuid4(), "access", secret=SECRET, lifetime=timedelta(minutes=5), now=past
    )
    with pytest.raises(tokens.TokenError):
        tokens.verify(tok, "access", secret=SECRET)


def test_wrong_secret_and_tampered_token_rejected() -> None:
    tok = tokens.issue(uuid.uuid4(), "access", secret=SECRET, lifetime=timedelta(minutes=5))
    with pytest.raises(tokens.TokenError):
        tokens.verify(tok, "access", secret="y" * 40)
    head, payload, sig = tok.split(".")
    with pytest.raises(tokens.TokenError):
        tokens.verify(f"{head}.{payload}.{sig[:-2]}AA", "access", secret=SECRET)


def test_alg_none_token_rejected() -> None:
    forged = jwt.encode(
        {"sub": str(uuid.uuid4()), "typ": "access", "iat": datetime.now(UTC),
         "exp": datetime.now(UTC) + timedelta(minutes=5)},
        key=None,
        algorithm="none",
    )
    with pytest.raises(tokens.TokenError):
        tokens.verify(forged, "access", secret=SECRET)


@pytest.mark.parametrize("garbage", ["", "abc", "a.b.c"])
def test_garbage_token_rejected(garbage: str) -> None:
    with pytest.raises(tokens.TokenError):
        tokens.verify(garbage, "access", secret=SECRET)


def test_token_missing_required_claims_rejected() -> None:
    no_typ = jwt.encode(
        {"sub": str(uuid.uuid4()), "iat": datetime.now(UTC),
         "exp": datetime.now(UTC) + timedelta(minutes=5)},
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(tokens.TokenError):
        tokens.verify(no_typ, "access", secret=SECRET)


def test_permission_catalogue_and_builtins() -> None:
    assert is_valid_permission("documents:read")
    assert not is_valid_permission("documents:launch-missiles")
    assert BUILTIN_ROLES["admin"][1] == frozenset(Permission)
    assert Permission.USERS_MANAGE not in BUILTIN_ROLES["user"][1]
    assert Permission.DOCUMENTS_DELETE not in BUILTIN_ROLES["user"][1]
    assert Permission.DOCUMENTS_WRITE not in BUILTIN_ROLES["viewer"][1]
