"""API keys for machine clients (no HTTP knowledge).

A key looks like ``rag_<prefix>_<secret>``:

* ``prefix`` - 8 chars of ``[a-z0-9]``, unique, used only to find the row (indexed lookup);
* ``secret`` - ``secrets.token_urlsafe(32)``: 256 bits from the OS CSPRNG.

Only ``sha256(secret)`` is stored. SHA-256 (rather than scrypt/argon2) is deliberate: slow KDFs exist to make
guessing *low-entropy human passwords* expensive; a 256-bit random secret cannot be guessed or brute-forced from
its hash whatever the hash speed, and a slow hash would add CPU latency to every machine request. The hash is
compared in constant time (``hmac.compare_digest``). Permissions are not stored on the key: they come from the
bound role, read on every request, so role edits, revocation, expiry and owner deactivation apply immediately.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import string
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApiKey, Role, User
from app.services.auth.accounts import ConflictError, InvalidError
from app.services.auth.permissions import ADMIN_ROLE, KEY_FORBIDDEN

KEY_TAG = "rag_"
PREFIX_LEN = 8
_PREFIX_ALPHABET = string.ascii_lowercase + string.digits
SECRET_BYTES = 32
#: ``last_used_at`` is written at most once per this interval per key (no write per request).
LAST_USED_THROTTLE = timedelta(seconds=60)
MAX_EXPIRY_DAYS = 3650
_MAX_PREFIX_ATTEMPTS = 8

#: Compared against when the prefix is unknown so lookup misses cost the same as secret mismatches.
_DUMMY_HASH = hashlib.sha256(b"rag-api-key-dummy").hexdigest()


class InvalidKeyError(Exception):
    """Any reason a presented key is not acceptable. Deliberately carries no detail (no oracle)."""


def utcnow() -> datetime:
    """Naive UTC, matching the other timestamp columns."""
    return datetime.now(UTC).replace(tzinfo=None)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def generate() -> tuple[str, str]:
    """Return ``(prefix, secret)``."""
    prefix = "".join(secrets.choice(_PREFIX_ALPHABET) for _ in range(PREFIX_LEN))
    return prefix, secrets.token_urlsafe(SECRET_BYTES)


def format_key(prefix: str, secret: str) -> str:
    return f"{KEY_TAG}{prefix}_{secret}"


def looks_like_key(value: str) -> bool:
    return value.startswith(KEY_TAG)


def parse(raw: str) -> tuple[str, str] | None:
    """Split ``rag_<prefix>_<secret>``; None when malformed. The prefix has a fixed length (secrets may contain ``_``)."""
    raw = raw.strip()
    head = len(KEY_TAG) + PREFIX_LEN
    if not raw.startswith(KEY_TAG) or len(raw) <= head + 1 or raw[head] != "_":
        return None
    prefix, secret = raw[len(KEY_TAG) : head], raw[head + 1 :]
    if any(c not in _PREFIX_ALPHABET for c in prefix):
        return None
    return prefix, secret


def is_usable(key: ApiKey, now: datetime) -> bool:
    """Not revoked, not expired, owner still active."""
    if key.revoked_at is not None:
        return False
    if key.expires_at is not None and now >= key.expires_at:
        return False
    return bool(key.owner.is_active)


def permissions_of(key: ApiKey) -> frozenset[str]:
    """The bound role's permissions minus management permissions; a key never exceeds its role."""
    return frozenset(p.permission for p in key.role.permissions) - KEY_FORBIDDEN


def expiry_for(days: int | None, now: datetime) -> datetime | None:
    if days is None:
        return None
    if not (1 <= days <= MAX_EXPIRY_DAYS):
        raise InvalidError(f"expires_in_days must be between 1 and {MAX_EXPIRY_DAYS}")
    return now + timedelta(days=days)


# ── lifecycle ────────────────────────────────────────────────────────────────


async def create(
    session: AsyncSession,
    *,
    name: str,
    owner: User,
    role: Role,
    creator_permissions: frozenset[str],
    expires_in_days: int | None = None,
    now: datetime | None = None,
) -> tuple[ApiKey, str]:
    """Create a key; returns ``(row, full_key)``. The full key exists only in this return value."""
    now = now or utcnow()
    name = name.strip()
    if not (1 <= len(name) <= 64):
        raise InvalidError("name must be 1-64 characters")
    expires_at = expiry_for(expires_in_days, now)
    role_perms = {p.permission for p in role.permissions}
    if not role_perms <= creator_permissions:
        raise ConflictError(
            "role has permissions you do not hold: " + ", ".join(sorted(role_perms - creator_permissions))
        )
    owner_id, role_id = owner.id, role.id  # a rollback below expires the ORM objects
    for _ in range(_MAX_PREFIX_ATTEMPTS):
        prefix, secret = generate()
        row = ApiKey(
            name=name,
            prefix=prefix,
            secret_hash=hash_secret(secret),
            owner_user_id=owner_id,
            role_id=role_id,
            created_at=now,
            expires_at=expires_at,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:  # prefix collision (unique index): draw again
            await session.rollback()
            continue
        await session.refresh(row)
        return row, format_key(prefix, secret)
    raise ConflictError("could not allocate a unique key prefix")  # pragma: no cover (2^-40 per attempt)


def warnings_for(role: Role) -> list[str]:
    if role.name == ADMIN_ROLE:
        return [
            "This key is bound to the admin role: it can read, write and delete everything. "
            "Management permissions (users, roles, API keys) are never available to keys. "
            "Prefer a narrow role such as one with only search:use."
        ]
    return []


async def list_keys(session: AsyncSession) -> list[ApiKey]:
    stmt = select(ApiKey).order_by(ApiKey.created_at.desc()).execution_options(populate_existing=True)
    return list((await session.execute(stmt)).unique().scalars())


async def get(session: AsyncSession, key_id: uuid.UUID) -> ApiKey | None:
    return await session.get(ApiKey, key_id, populate_existing=True)


async def revoke(session: AsyncSession, key: ApiKey, *, now: datetime | None = None) -> ApiKey:
    """Idempotent: revoking again keeps the original ``revoked_at``."""
    if key.revoked_at is None:
        key.revoked_at = now or utcnow()
        await session.commit()
        await session.refresh(key)
    return key


# ── authentication ───────────────────────────────────────────────────────────


async def authenticate(session: AsyncSession, raw: str, *, now: datetime | None = None) -> ApiKey:
    """Return the usable key for ``raw`` or raise :class:`InvalidKeyError`.

    The row (with owner and role+permissions) is re-read from the database every call
    (``populate_existing``), so nothing is ever served from a stale identity map.
    """
    now = now or utcnow()
    parsed = parse(raw)
    if parsed is None:
        hmac.compare_digest(_DUMMY_HASH, hash_secret(raw))  # keep timing alike
        raise InvalidKeyError
    prefix, secret = parsed
    stmt = select(ApiKey).where(ApiKey.prefix == prefix).execution_options(populate_existing=True)
    key = (await session.execute(stmt)).unique().scalar_one_or_none()
    expected = key.secret_hash if key is not None else _DUMMY_HASH
    matches = hmac.compare_digest(expected, hash_secret(secret))
    if key is None or not matches:
        raise InvalidKeyError
    if not is_usable(key, now):
        raise InvalidKeyError
    return key


async def touch(session: AsyncSession, key: ApiKey, *, now: datetime | None = None) -> bool:
    """Record ``last_used_at`` at most once per :data:`LAST_USED_THROTTLE`. True when a write happened.

    A single conditional UPDATE, so concurrent requests cannot both write inside the window.
    """
    now = now or utcnow()
    stmt = (
        update(ApiKey)
        .where(
            ApiKey.id == key.id,
            or_(ApiKey.last_used_at.is_(None), ApiKey.last_used_at <= now - LAST_USED_THROTTLE),
        )
        .values(last_used_at=now)
    )
    result = await session.execute(stmt)
    if getattr(result, "rowcount", 0):
        await session.commit()
        return True
    return False
