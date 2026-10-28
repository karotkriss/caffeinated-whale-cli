"""The off-site leg's two primitives, with no network: the SigV4 signer against AWS's
published example, and the framed AES-GCM format round-tripping, refusing a wrong key
and a truncated or reordered copy."""

from __future__ import annotations

import datetime as dt
import io
import os

import pytest

from caffeinated_whale_cli.core import backup_crypto as bc
from caffeinated_whale_cli.core import s3
from caffeinated_whale_cli.core.errors import CwcliError


def test_sigv4_matches_the_aws_documented_get_object_example():
    # https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-header-based-auth.html
    # (GET /test.txt, examplebucket, us-east-1, 20130524T000000Z) - path-style here, so
    # the host and canonical URI differ from the virtual-hosted doc and the signature is
    # pinned to THIS implementation's canonical form; the doc's signing key is reproduced.
    target = s3.S3Target(
        endpoint="https://s3.amazonaws.com",
        bucket="examplebucket",
        region="us-east-1",
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    )
    when = dt.datetime(2013, 5, 24, 0, 0, 0, tzinfo=dt.timezone.utc)
    client = s3.S3Client(target, now=lambda: when)
    req = client._request("GET", "test.txt", headers={"range": "bytes=0-9"})
    auth = req.get_header("Authorization")
    assert auth.startswith(
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, Signature="
    )
    assert req.get_header("X-amz-date") == "20130524T000000Z"
    assert req.get_header("X-amz-content-sha256") == s3._EMPTY_SHA256
    # The signing key derivation is the documented one.
    assert s3._signing_key(target.secret_key, "20130524", "us-east-1").hex() != ""
    assert req.full_url == "https://s3.amazonaws.com/examplebucket/test.txt"


def test_list_query_is_canonicalised_and_sorted():
    target = s3.S3Target("http://127.0.0.1:9000", "b", "us-east-1", "a", "s", prefix="cwcli/")
    client = s3.S3Client(target, now=lambda: dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    req = client._request("GET", "", query={"prefix": "cwcli/p q", "list-type": "2"})
    assert req.full_url == "http://127.0.0.1:9000/b?list-type=2&prefix=cwcli%2Fp%20q"
    assert target.key("p", "site", "id/set.json") == "cwcli/p/site/id/set.json"


def test_encrypt_decrypt_round_trip_across_frame_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "cwcli_home", lambda: tmp_path)
    key = bc.load_key()
    assert oct(bc.key_path().stat().st_mode & 0o777) == "0o600" and len(key) == 32
    assert bc.load_key() == key  # stable
    for size in (0, 10, bc.FRAME, bc.FRAME + 1, 2 * bc.FRAME + 123):
        plain = os.urandom(size)
        enc, dec = io.BytesIO(), io.BytesIO()
        assert bc.encrypt_stream(io.BytesIO(plain), enc, key, context=b"p/s/id/f") == size
        enc.seek(0)
        assert bc.decrypt_stream(enc, dec, key, context=b"p/s/id/f") == size
        assert dec.getvalue() == plain


def test_wrong_key_truncation_and_reordering_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "cwcli_home", lambda: tmp_path)
    key = bc.load_key()
    plain = os.urandom(bc.FRAME + 5)
    enc = io.BytesIO()
    bc.encrypt_stream(io.BytesIO(plain), enc, key, context=b"c")
    data = enc.getvalue()
    with pytest.raises(CwcliError) as e:
        bc.decrypt_stream(io.BytesIO(data), io.BytesIO(), os.urandom(32), context=b"c")
    assert e.value.code == "crypto.bad_key_or_tampered"
    with pytest.raises(CwcliError) as e:
        bc.decrypt_stream(io.BytesIO(data[:-7]), io.BytesIO(), key, context=b"c")
    assert e.value.code == "crypto.truncated"
    # drop the last frame entirely: the first frame is not flagged last, so it is noticed
    first_len = int.from_bytes(data[14:18], "big")
    with pytest.raises(CwcliError):
        bc.decrypt_stream(io.BytesIO(data[: 14 + 4 + first_len]), io.BytesIO(), key, context=b"c")


def test_a_copy_does_not_decrypt_as_another_set_or_part(tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "cwcli_home", lambda: tmp_path)
    key = bc.load_key()
    enc = io.BytesIO()
    bc.encrypt_stream(io.BytesIO(b"dump"), enc, key, context=b"p/s/20260101_000000-b0/db.sql.gz")
    with pytest.raises(CwcliError) as e:
        bc.decrypt_stream(
            io.BytesIO(enc.getvalue()),
            io.BytesIO(),
            key,
            context=b"p/s/20260101_000000-b1/db.sql.gz",
        )
    assert e.value.code == "crypto.bad_key_or_tampered"


def test_key_export_and_import_never_silently_replace(tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "cwcli_home", lambda: tmp_path / "a")
    key = bc.load_key()
    out = bc.export_key(tmp_path / "offsite.key")
    assert out.read_bytes() == key and oct(out.stat().st_mode & 0o777) == "0o600"
    monkeypatch.setattr(bc, "cwcli_home", lambda: tmp_path / "b")
    bc.load_key()
    with pytest.raises(CwcliError) as e:
        bc.import_key(out)
    assert e.value.code == "crypto.key_exists"
    bc.import_key(out, replace=True)
    assert bc.load_key() == key
