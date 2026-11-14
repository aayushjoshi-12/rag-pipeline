"""ensure_bucket error handling with a stubbed S3 client (no network): only 'missing' triggers creation."""

from __future__ import annotations

from typing import Any

import pytest
from app.config import Settings
from app.services import storage
from botocore.exceptions import ClientError


class FakeClient:
    def __init__(self, head_code: str | None) -> None:
        self.head_code = head_code
        self.created: list[dict[str, Any]] = []

    def head_bucket(self, Bucket: str) -> None:
        if self.head_code is not None:
            raise ClientError({"Error": {"Code": self.head_code, "Message": "x"}}, "HeadBucket")

    def create_bucket(self, **params: Any) -> None:
        self.created.append(params)


def _cfg(**kw: Any) -> Settings:
    return Settings(storage_backend="s3", s3_bucket="b", auth_enabled=False, **kw)


@pytest.mark.parametrize("code", ["404", "NoSuchBucket", "NotFound"])
def test_missing_bucket_is_created(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    fake = FakeClient(code)
    monkeypatch.setattr(storage, "make_s3_client", lambda _s: fake)
    assert storage.ensure_bucket(_cfg()) == "created"
    assert fake.created == [{"Bucket": "b"}]  # us-east-1: no LocationConstraint


@pytest.mark.parametrize("code", ["403", "AccessDenied", "500", "InternalError"])
def test_other_errors_propagate_and_nothing_is_created(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    fake = FakeClient(code)
    monkeypatch.setattr(storage, "make_s3_client", lambda _s: fake)
    with pytest.raises(ClientError):
        storage.ensure_bucket(_cfg())
    assert fake.created == []  # a permissions problem must never be "fixed" by trying to create the bucket


def test_existing_bucket_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient(None)
    monkeypatch.setattr(storage, "make_s3_client", lambda _s: fake)
    assert storage.ensure_bucket(_cfg()) == "exists"
    assert fake.created == []


def test_non_default_region_sends_a_location_constraint(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient("404")
    monkeypatch.setattr(storage, "make_s3_client", lambda _s: fake)
    storage.ensure_bucket(_cfg(s3_region="eu-west-1"))
    assert fake.created == [{"Bucket": "b", "CreateBucketConfiguration": {"LocationConstraint": "eu-west-1"}}]
