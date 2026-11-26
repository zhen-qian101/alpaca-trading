"""Where web/publisher.py puts its objects: a Cloudflare R2 bucket (S3 API) or a
local folder laid out exactly like the bucket.

Both destinations speak the same five verbs (put bytes, put a file, get, list,
delete) and both refuse any key that is not a plain relative path, so a key
built from data (an archive file name, a symbol) can never climb out of the
bucket prefix or the output folder.

boto3 is imported LAZILY, inside R2Destination.from_config, and nowhere else.
The local-folder mode, the tests and every import of this module work without
it; only a real upload needs boto3 (the `publish` extra in pyproject.toml,
pinned exactly in requirements/constraints.txt).

Secrets: R2Config keeps the secret key out of its repr, and nothing here logs a
key, a secret or a presigned URL. Errors name the missing VARIABLE, never a
value.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

# A bucket key: "/"-separated segments, each starting with a letter or digit.
# That one rule already excludes "", ".", ".." and dot-led names, and the
# character class excludes "%", "\\" and whitespace, so no encoded or Windows
# traversal can be spelled either.
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$")
MAX_KEY_LEN = 512

# Cloudflare account ids are 32 hex characters. Validated because the id is
# spliced into the endpoint host name.
_ACCOUNT_RE = re.compile(r"^[0-9a-fA-F]{32}$")
# R2 bucket naming rules: 3-63 chars, lower-case letters, digits and hyphens.
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")

DEFAULT_BUCKET = "alpaca-dashboard"
ENDPOINT_TEMPLATE = "https://{account}.r2.cloudflarestorage.com"

# Large files go up as a multipart upload through boto3's managed transfer.
# R2 needs every part but the last to be the same size and at least 5 MiB.
MULTIPART_THRESHOLD = 32 * 1024 * 1024
MULTIPART_CHUNKSIZE = 16 * 1024 * 1024


class ConfigError(RuntimeError):
    """The R2 destination is not configured (or boto3 is not installed)."""


class DestinationError(RuntimeError):
    """A destination call failed in a way the caller should report."""


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    etag: str | None = None


def check_key(key: str) -> str:
    """Return `key` if it is a plain relative bucket key, else raise ValueError."""
    if not isinstance(key, str) or len(key) > MAX_KEY_LEN or not _KEY_RE.fullmatch(key):
        raise ValueError(f"refusing unsafe object key {key!r}")
    return key


# --------------------------------------------------------------------------
# local folder
# --------------------------------------------------------------------------
class DirDestination:
    """The bucket layout written to a local folder (dev mode and tests).

    Writes are atomic (temp file + os.replace) so a reader, such as a seeding
    script, never sees half a file.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()

    @property
    def ident(self) -> str:
        return f"dir:{self.root}"

    def describe(self) -> str:
        return f"directory {self.root}"

    def _path(self, key: str) -> Path:
        p = (self.root / check_key(key)).resolve()
        if not p.is_relative_to(self.root):
            raise ValueError(f"refusing key outside the output folder: {key!r}")
        return p

    def _write_atomic(self, dest: Path, writer) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                writer(fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, dest)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def put_bytes(self, key: str, data: bytes, content_type: str,
                  cache_control: str | None = None) -> None:
        self._write_atomic(self._path(key), lambda fh: fh.write(data))

    def put_file(self, key: str, src: Path, content_type: str,
                 cache_control: str | None = None) -> None:
        def copy(fh):
            with open(src, "rb") as inp:
                shutil.copyfileobj(inp, fh, 1024 * 1024)
        self._write_atomic(self._path(key), copy)

    def get_bytes(self, key: str, max_bytes: int) -> bytes | None:
        p = self._path(key)
        try:
            with open(p, "rb") as fh:
                return fh.read(max_bytes + 1)
        except FileNotFoundError:
            return None

    def list(self, prefix: str) -> list[ObjectInfo]:
        base = self._path(prefix.rstrip("/"))
        if not base.is_dir():
            return []
        out = []
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.name.startswith("."):
                continue
            key = p.relative_to(self.root).as_posix()
            try:
                check_key(key)
            except ValueError:
                continue
            st = p.stat()
            # A content-free change marker, like an S3 ETag: size + mtime.
            out.append(ObjectInfo(key, st.st_size, f"{st.st_size}-{st.st_mtime_ns}"))
        return out

    def delete(self, keys: list[str]) -> None:
        for key in keys:
            p = self._path(key)
            p.unlink(missing_ok=True)
            parent = p.parent
            while parent != self.root and parent.is_relative_to(self.root):
                try:
                    parent.rmdir()          # only succeeds when empty
                except OSError:
                    break
                parent = parent.parent


# --------------------------------------------------------------------------
# Cloudflare R2
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class R2Config:
    account_id: str
    access_key_id: str
    secret_access_key: str = field(repr=False)
    bucket: str = DEFAULT_BUCKET

    @property
    def endpoint(self) -> str:
        return ENDPOINT_TEMPLATE.format(account=self.account_id.lower())

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "R2Config":
        env = os.environ if env is None else env
        names = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
        values = {n: (env.get(n) or "").strip() for n in names}
        missing = [n for n, v in values.items() if not v]
        if missing:
            raise ConfigError(
                f"R2 is not configured: set {', '.join(missing)} in .env "
                "(or use --out-dir for a local folder)")
        bucket = (env.get("R2_BUCKET") or DEFAULT_BUCKET).strip()
        if not _ACCOUNT_RE.fullmatch(values["R2_ACCOUNT_ID"]):
            raise ConfigError("R2_ACCOUNT_ID must be the 32-character Cloudflare account id")
        if not _BUCKET_RE.fullmatch(bucket):
            raise ConfigError("R2_BUCKET is not a valid bucket name")
        return cls(values["R2_ACCOUNT_ID"], values["R2_ACCESS_KEY_ID"],
                   values["R2_SECRET_ACCESS_KEY"], bucket)


def _error_code(exc: BaseException) -> str:
    """The S3 error code of a botocore ClientError (or a test double), else ""."""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        return str((resp.get("Error") or {}).get("Code") or "")
    return ""


class R2Destination:
    """An R2 bucket through any boto3-compatible S3 client.

    The client is injected so tests can hand in a fake; `from_config` builds
    the real one.
    """

    def __init__(self, client, bucket: str, *, account_id: str = "",
                 transfer_config=None) -> None:
        self._client = client
        self.bucket = bucket
        self._account = account_id
        self._transfer_config = transfer_config

    @property
    def ident(self) -> str:
        return f"r2:{self._account.lower()}/{self.bucket}"

    def describe(self) -> str:
        return f"R2 bucket {self.bucket}"

    @classmethod
    def from_config(cls, cfg: R2Config) -> "R2Destination":
        try:
            import boto3
            from boto3.s3.transfer import TransferConfig
            from botocore.config import Config
        except ImportError as exc:
            raise ConfigError(
                "boto3 is not installed (the 'publish' extra): "
                ".venv/bin/python -m pip install -c requirements/constraints.txt boto3"
                ) from exc
        base = dict(
            region_name="auto",
            signature_version="s3v4",
            connect_timeout=10,
            read_timeout=60,
            retries={"max_attempts": 4, "mode": "standard"},
        )
        try:
            # botocore >= 1.36 adds CRC checksums to every upload by default;
            # Cloudflare's guidance for R2 is to send them only when required.
            config = Config(**base, request_checksum_calculation="when_required",
                            response_checksum_validation="when_required")
        except TypeError:                       # older botocore: no such option
            config = Config(**base)
        client = boto3.session.Session().client(
            "s3",
            endpoint_url=cfg.endpoint,
            aws_access_key_id=cfg.access_key_id,
            aws_secret_access_key=cfg.secret_access_key,
            config=config,
        )
        transfer = TransferConfig(multipart_threshold=MULTIPART_THRESHOLD,
                                  multipart_chunksize=MULTIPART_CHUNKSIZE,
                                  max_concurrency=4, use_threads=True)
        return cls(client, cfg.bucket, account_id=cfg.account_id, transfer_config=transfer)

    @staticmethod
    def _extra(content_type: str, cache_control: str | None) -> dict:
        extra = {"ContentType": content_type}
        if cache_control:
            extra["CacheControl"] = cache_control
        return extra

    def put_bytes(self, key: str, data: bytes, content_type: str,
                  cache_control: str | None = None) -> None:
        self._client.put_object(Bucket=self.bucket, Key=check_key(key), Body=data,
                                **self._extra(content_type, cache_control))

    def put_file(self, key: str, src: Path, content_type: str,
                 cache_control: str | None = None) -> None:
        """Managed transfer: multipart above MULTIPART_THRESHOLD, with retries."""
        kwargs = {"ExtraArgs": self._extra(content_type, cache_control)}
        if self._transfer_config is not None:
            kwargs["Config"] = self._transfer_config
        self._client.upload_file(str(src), self.bucket, check_key(key), **kwargs)

    def get_bytes(self, key: str, max_bytes: int) -> bytes | None:
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=check_key(key))
        except Exception as exc:                # noqa: BLE001 — narrowed just below
            if _error_code(exc) in ("NoSuchKey", "404", "NotFound"):
                return None
            raise
        body = resp["Body"]
        try:
            return body.read(max_bytes + 1)
        finally:
            close = getattr(body, "close", None)
            if close:
                close()

    def list(self, prefix: str) -> list[ObjectInfo]:
        check_key(prefix.rstrip("/"))
        out: list[ObjectInfo] = []
        token = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents") or []:
                etag = obj.get("ETag")
                out.append(ObjectInfo(obj["Key"], int(obj.get("Size") or 0),
                                      etag.strip('"') if isinstance(etag, str) else None))
            if not resp.get("IsTruncated"):
                return out
            token = resp.get("NextContinuationToken")
            if not token:
                return out

    def delete(self, keys: list[str]) -> None:
        keys = [check_key(k) for k in keys]
        for i in range(0, len(keys), 1000):        # the S3 API's batch ceiling
            batch = keys[i:i + 1000]
            resp = self._client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True})
            errors = (resp or {}).get("Errors") or []
            if errors:
                raise DestinationError(f"{len(errors)} of {len(batch)} deletes failed "
                                       f"(first: {errors[0].get('Code', '?')})")
