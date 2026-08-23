"""Authenticated CryptoHFTData hourly-file cache.

The API key is intentionally accepted only through CRYPTOHFTDATA_API_KEY in
the process environment. It is exchanged for a short-lived JWT and is never
placed in a URL, manifest, or log message.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

import pyarrow.parquet as pq
import zstandard as zstd


API_BASE = "https://api.cryptohftdata.com"


@dataclass(frozen=True, order=True)
class HourlyObject:
    exchange: str
    hour: datetime
    symbol: str
    data_type: str

    def __post_init__(self) -> None:
        if self.hour.tzinfo is None or self.hour.utcoffset() != timezone.utc.utcoffset(self.hour):
            raise ValueError("hour must be timezone-aware UTC")
        if self.hour.minute or self.hour.second or self.hour.microsecond:
            raise ValueError("hour must be aligned to an exact UTC hour")
        if not self.exchange or not self.symbol or not self.data_type:
            raise ValueError("hourly object fields cannot be empty")

    @property
    def object_path(self) -> str:
        return (
            f"{self.exchange}/{self.hour:%Y-%m-%d}/{self.hour:%H}/"
            f"{self.symbol}_{self.data_type}.parquet.zst"
        )

    @property
    def hour_utc(self) -> str:
        return self.hour.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class CacheEntry:
    object_path: str
    hour_utc: str
    compressed_path: str
    parquet_path: str
    compressed_bytes: int
    parquet_bytes: int
    sha256: str
    parquet_rows: int
    parquet_row_groups: int
    schema_sha256: str


def _canonical_json(payload: Any) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode()


def _write_if_changed(path: Path, payload: bytes) -> None:
    if path.exists() and path.read_bytes() == payload:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class CryptoHFTDataClient:
    """Small JWT client with retry and resumable hourly downloads."""

    def __init__(self, *, timeout_seconds: float = 120.0, attempts: int = 5) -> None:
        api_key = os.environ.get("CRYPTOHFTDATA_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("CRYPTOHFTDATA_API_KEY is missing from the environment")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._attempts = attempts
        self._jwt: str | None = None

    def _issue_jwt(self) -> str:
        request = urllib.request.Request(
            f"{API_BASE}/jwt-token",
            data=b"{}",
            headers={"Content-Type": "application/json", "X-API-Key": self._api_key},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
            payload = json.load(response)
        token = payload.get("jwt_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("CryptoHFTData JWT response did not contain jwt_token")
        self._jwt = token
        return token

    def _request(self, object_path: str, offset: int) -> urllib.response.addinfourl:
        token = self._jwt or self._issue_jwt()
        query = urllib.parse.urlencode({"file": object_path})
        headers = {"Authorization": f"Bearer {token}"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        return urllib.request.urlopen(
            urllib.request.Request(f"{API_BASE}/download?{query}", headers=headers),
            timeout=self._timeout_seconds,
        )

    def download(self, object_path: str, destination: Path) -> bool:
        """Download to an atomic cache path; retain partial bytes on failure."""
        if destination.exists() and destination.stat().st_size > 0:
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        last_error: Exception | None = None
        for attempt in range(self._attempts):
            offset = partial.stat().st_size if partial.exists() else 0
            try:
                with self._request(object_path, offset) as response:
                    status = response.status
                    append = offset > 0 and status == 206
                    mode = "ab" if append else "wb"
                    expected_transfer = response.headers.get("Content-Length")
                    before = offset if append else 0
                    with partial.open(mode) as output:
                        shutil.copyfileobj(response, output, 1024 * 1024)
                    transferred = partial.stat().st_size - before
                    if expected_transfer is not None and transferred != int(expected_transfer):
                        raise OSError(
                            f"incomplete download body: expected {expected_transfer} bytes, "
                            f"received {transferred}"
                        )
                if partial.stat().st_size == 0:
                    raise RuntimeError("download returned an empty object")
                os.replace(partial, destination)
                return True
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code == 401:
                    self._jwt = None
                if error.code == 404:
                    raise FileNotFoundError(object_path) from error
                if error.code not in {401, 408, 429, 500, 502, 503, 504}:
                    raise RuntimeError(f"download failed with HTTP {error.code}") from error
            except (TimeoutError, urllib.error.URLError, OSError) as error:
                last_error = error
            if attempt + 1 < self._attempts:
                time.sleep(min(2**attempt, 8))
        raise RuntimeError(
            f"download failed after {self._attempts} attempts: {type(last_error).__name__}"
        ) from last_error


class HourlyCache:
    def __init__(self, root: Path, client: CryptoHFTDataClient) -> None:
        self.root = root.resolve()
        self.client = client

    def compressed_path(self, spec: HourlyObject) -> Path:
        return self.root / spec.object_path

    def parquet_path(self, spec: HourlyObject) -> Path:
        return self.compressed_path(spec).with_suffix("")

    def _decompress(self, compressed: Path, parquet: Path) -> None:
        temporary = parquet.with_suffix(parquet.suffix + ".part")
        with compressed.open("rb") as source, temporary.open("wb") as output:
            with zstd.ZstdDecompressor().stream_reader(source) as reader:
                shutil.copyfileobj(reader, output, 1024 * 1024)
        try:
            pq.ParquetFile(temporary)
        except Exception:
            # A failed artifact is evidence useful for diagnosis; do not delete it.
            failed = temporary.with_suffix(temporary.suffix + ".failed")
            os.replace(temporary, failed)
            raise
        os.replace(temporary, parquet)

    def ensure(self, spec: HourlyObject) -> CacheEntry:
        compressed = self.compressed_path(spec)
        parquet = self.parquet_path(spec)
        sidecar = compressed.with_suffix(compressed.suffix + ".manifest.json")
        if compressed.exists() and sidecar.exists():
            previous = json.loads(sidecar.read_text())
            if (
                previous.get("compressed_bytes") != compressed.stat().st_size
                or previous.get("sha256") != _sha256(compressed)
            ):
                raise RuntimeError(f"cached compressed object failed its manifest: {spec.object_path}")
        self.client.download(spec.object_path, compressed)
        if not parquet.exists():
            self._decompress(compressed, parquet)
        parquet_file = pq.ParquetFile(parquet)
        schema_text = parquet_file.schema_arrow.to_string(show_field_metadata=True)
        entry = CacheEntry(
            object_path=spec.object_path,
            hour_utc=spec.hour_utc,
            compressed_path=str(compressed.relative_to(self.root)),
            parquet_path=str(parquet.relative_to(self.root)),
            compressed_bytes=compressed.stat().st_size,
            parquet_bytes=parquet.stat().st_size,
            sha256=_sha256(compressed),
            parquet_rows=parquet_file.metadata.num_rows,
            parquet_row_groups=parquet_file.metadata.num_row_groups,
            schema_sha256=hashlib.sha256(schema_text.encode()).hexdigest(),
        )
        _write_if_changed(sidecar, _canonical_json(asdict(entry)))
        return entry

    def write_manifest(self, path: Path, entries: list[CacheEntry]) -> None:
        stable = sorted((asdict(entry) for entry in entries), key=lambda item: item["object_path"])
        _write_if_changed(path, _canonical_json({"format_version": 1, "objects": stable}))
