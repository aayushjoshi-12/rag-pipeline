from __future__ import annotations

import pytest
from app.config import Settings
from app.services.storage import (
    LocalStorage,
    S3Storage,
    StorageError,
    get_storage,
    key_dir,
    new_key,
)


def test_defaults_are_local_and_private_blocked() -> None:
    s = Settings(auth_enabled=False)
    assert s.storage_backend == "local" and s.url_ingest_allow_private is False
    assert s.url_schemes == ("http", "https", "s3") and s.url_max_bytes == s.max_upload_size_bytes
    assert isinstance(get_storage(s), LocalStorage)


@pytest.mark.parametrize(
    "kw",
    [
        {"storage_backend": "gcs"},
        {"storage_backend": "s3"},  # no bucket
        {"s3_access_key_id": "a"},  # half a credential pair
        {"s3_endpoint_url": "ftp://x"},
        {"s3_prefix": "a/../b"},
        {"url_ingest_schemes": "ftp"},
        {"url_ingest_schemes": ""},
        {"url_ingest_timeout_seconds": 0},
        {"url_ingest_timeout_seconds": 30, "url_ingest_total_timeout_seconds": 10},
        {"url_ingest_max_redirects": 99},
        {"url_ingest_max_bytes": 0},
    ],
)
def test_invalid_storage_settings_fail_fast(kw: dict[str, object]) -> None:
    with pytest.raises(ValueError):  # SettingsError is a ValueError (pydantic wraps it)
        Settings(auth_enabled=False, **kw)  # type: ignore[arg-type]


def test_s3_prefix_normalisation_and_bucket_allow_list() -> None:
    s = Settings(auth_enabled=False, s3_bucket="b", s3_prefix="/x/y")
    assert s.s3_key_prefix == "x/y/" and s.url_s3_buckets == ("b",)
    assert Settings(auth_enabled=False, s3_bucket="b", url_ingest_s3_buckets="p, q").url_s3_buckets == ("p", "q")
    assert Settings(auth_enabled=False, s3_prefix="").s3_key_prefix == ""


def test_s3_credentials_are_secret_fields() -> None:
    from app.config import SECRET_FIELD_NAMES

    assert {"s3_access_key_id", "s3_secret_access_key"} <= SECRET_FIELD_NAMES


def test_key_helpers() -> None:
    k = new_key("a.pdf")
    assert key_dir(k) == k.rsplit("/", 1)[0]
    assert key_dir("uploads/short/a.pdf") is None and key_dir("other/aaaa/x") is None
    assert key_dir("uploads/" + "g" * 32 + "/x") is None  # not hex


def test_s3_storage_refuses_foreign_keys() -> None:
    st = S3Storage(Settings(auth_enabled=False, s3_bucket="b"), client=object())
    with pytest.raises(StorageError):
        st.delete_object("other/thing")
    with pytest.raises(StorageError):
        st.delete_object("uploads/" + "a" * 32 + "/nested/x")
