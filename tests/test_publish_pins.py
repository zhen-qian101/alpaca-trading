"""The publish extra (boto3) is pinned, installed pinned, and works as pinned.

boto3 goes into the production venv, next to the live broker keys, so it is
held to the same rule as everything else there: exact versions from
requirements/constraints.txt, installed with `-c` so nothing it shares with
the trading path (urllib3, python-dateutil) can move. CI installs the extra,
so the real-client tests below run against exactly those versions; without
boto3 (the production venv before the extra is installed) they skip.

No network: the real boto3 client is driven through botocore's Stubber.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

from web.r2 import MULTIPART_THRESHOLD, R2Config, R2Destination

ROOT = Path(__file__).resolve().parent.parent
PUBLISH_STACK = ("boto3", "botocore", "s3transfer", "jmespath")
_PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s#]+)\s*(?:#.*)?$")


def _pins() -> dict[str, str]:
    pins = {}
    for line in (ROOT / "requirements" / "constraints.txt").read_text().splitlines():
        m = _PIN_RE.match(line.strip())
        if m:
            pins[m.group(1).lower().replace("_", "-")] = m.group(2)
    return pins


def test_the_publish_stack_is_pinned_exactly():
    pins = _pins()
    missing = [name for name in PUBLISH_STACK if name not in pins]
    assert not missing, f"requirements/constraints.txt does not pin {missing}"
    # botocore shares these with the trading path; they must be pinned too,
    # or `pip install -c ... boto3` could still move them.
    assert {"urllib3", "python-dateutil"} <= pins.keys()
    assert pins["boto3"] == pins["botocore"], "boto3 and botocore are released in lockstep"


@pytest.mark.parametrize("doc", ["README.md", "docs/cloud-dashboard.md", "web/r2.py",
                                 "requirements/constraints.txt"])
def test_every_documented_install_uses_the_constraints(doc):
    """No install line may pull boto3 without the pins (SEC-4: the old line
    was `pip install 'boto3>=1.34'`, i.e. whatever PyPI had that day)."""
    text = (ROOT / doc).read_text()
    lines = [ln for ln in text.splitlines() if "pip install" in ln and "boto3" in ln]
    assert lines, f"{doc} no longer documents the install"
    for ln in lines:
        assert "-c requirements/constraints.txt" in ln, f"{doc}: unpinned install: {ln.strip()}"
        assert ">=" not in ln, f"{doc}: a version range in the install line: {ln.strip()}"


def test_ci_installs_the_publish_extra_with_the_constraints():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    installs = [ln for ln in ci.splitlines() if "pip install" in ln and "-e" in ln]
    assert installs and all("-c requirements/constraints.txt" in ln for ln in installs)
    assert any("publish" in ln for ln in installs)


# --------------------------------------------------------------------------
# with the pinned boto3 itself (CI; skipped where the extra is not installed)
# --------------------------------------------------------------------------
def test_the_installed_publish_stack_is_the_pinned_one():
    pytest.importorskip("boto3")
    from importlib.metadata import version

    pins = _pins()
    assert {name: version(name) for name in PUBLISH_STACK} == {
        name: pins[name] for name in PUBLISH_STACK}


@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    pytest.importorskip("boto3")
    from botocore.stub import Stubber

    # Hermetic: no shared AWS config, profile or credentials can leak in.
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                 "AWS_SESSION_TOKEN", "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))
    cfg = R2Config("0123456789abcdef0123456789abcdef", "synthetic-key-id", "synthetic-secret",
                   "alpaca-dashboard")
    dest = R2Destination.from_config(cfg)
    with Stubber(dest._client) as stub:
        yield dest, stub
        stub.assert_no_pending_responses()


def test_the_real_client_is_built_for_r2(stubbed):
    dest, _ = stubbed
    meta = dest._client.meta
    assert meta.endpoint_url == "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com"
    assert meta.region_name == "auto"
    # The pinned botocore knows the checksum options (no TypeError fallback),
    # so uploads carry no CRC trailer R2 does not ask for.
    assert meta.config.request_checksum_calculation == "when_required"
    assert meta.config.response_checksum_validation == "when_required"
    assert dest._transfer_config.multipart_threshold == MULTIPART_THRESHOLD


def test_the_real_client_speaks_every_verb_the_publisher_uses(stubbed, tmp_path):
    from botocore.response import StreamingBody

    dest, stub = stubbed
    bucket = "alpaca-dashboard"
    stub.add_response("put_object", {}, {
        "Bucket": bucket, "Key": "snapshot/latest.json", "Body": b"{}",
        "ContentType": "application/json", "CacheControl": "private, no-cache"})
    dest.put_bytes("snapshot/latest.json", b"{}", "application/json", "private, no-cache")

    body = b'{"date":"2026-03-04"}'
    stub.add_response("get_object", {"Body": StreamingBody(io.BytesIO(body), len(body))},
                      {"Bucket": bucket, "Key": "approvals/2026-03-04.json"})
    assert dest.get_bytes("approvals/2026-03-04.json", 4096) == body
    stub.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404,
                          expected_params={"Bucket": bucket, "Key": "approvals/2026-03-05.json"})
    assert dest.get_bytes("approvals/2026-03-05.json", 4096) is None

    stub.add_response("list_objects_v2", {
        "IsTruncated": True, "NextContinuationToken": "t1", "KeyCount": 1,
        "Contents": [{"Key": "media/2026-03-04/video/a.mp4", "Size": 5, "ETag": '"e1"'}]},
        {"Bucket": bucket, "Prefix": "media/"})
    stub.add_response("list_objects_v2", {
        "IsTruncated": False, "KeyCount": 1,
        "Contents": [{"Key": "media/history.json", "Size": 2, "ETag": '"e2"'}]},
        {"Bucket": bucket, "Prefix": "media/", "ContinuationToken": "t1"})
    assert [(o.key, o.etag) for o in dest.list("media/")] == [
        ("media/2026-03-04/video/a.mp4", "e1"), ("media/history.json", "e2")]

    stub.add_response("delete_objects", {"Deleted": [{"Key": "media/2026-03-04/video/a.mp4"}]}, {
        "Bucket": bucket, "Delete": {"Objects": [{"Key": "media/2026-03-04/video/a.mp4"}],
                                     "Quiet": True}})
    dest.delete(["media/2026-03-04/video/a.mp4"])


def test_the_real_managed_transfer_uploads_a_small_file_in_one_put(stubbed, tmp_path):
    from botocore.stub import ANY

    dest, stub = stubbed
    src = tmp_path / "cover.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(64))
    # Exactly these parameters: with request_checksum_calculation="when_required"
    # the transfer adds no ChecksumAlgorithm, which R2 does not need.
    stub.add_response("put_object", {}, {
        "Bucket": "alpaca-dashboard", "Key": "media/2026-03-04/video/cover.png", "Body": ANY,
        "ContentType": "image/png", "CacheControl": "private, max-age=300"})
    dest.put_file("media/2026-03-04/video/cover.png", src, "image/png", "private, max-age=300")
