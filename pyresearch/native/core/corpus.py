"""Frozen definition of the native_dev_v1 development corpus.

The three raw files are three separate collector processes, each with its own REST snapshot.
They are replayed independently and are never concatenated into one continuous order book.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import subprocess

from pyresearch import ROOT
RAW_DIR = ROOT / "data/raw/aws_london/native_dev_v1"
SPEC_PATH = ROOT / "research/specs/native_dev_v1.json"
# Heavy derived datasets follow the existing repository convention and live under the ignored
# data/ tree; only small, reviewable artifacts are committed under research/.
DATASET_DIR = ROOT / "data/research/native_dev_v1"
REPORT_DIR = ROOT / "research/native_dev_v1"

SCHEMA = "crypto-hft-native-dev-corpus-v1"


@dataclass(frozen=True)
class CorpusFile:
    file_index: int
    name: str

    @property
    def raw_path(self) -> Path:
        return RAW_DIR / self.name

    @property
    def dataset_path(self) -> Path:
        return DATASET_DIR / f"native_features_100ms_file{self.file_index}.csv.zst"

    @property
    def qc_path(self) -> Path:
        return REPORT_DIR / f"qc_file{self.file_index}.json"


CORPUS: tuple[CorpusFile, ...] = (
    CorpusFile(0, "BTCUSDT-LONDON-20260817T210035Z.chft.zst"),
    CorpusFile(1, "BTCUSDT-LONDON-20260817T221753Z.chft.zst"),
    CorpusFile(2, "BTCUSDT-LONDON-20260818T062918Z.chft.zst"),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def git_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def load_qc() -> list[dict]:
    return [json.loads(entry.qc_path.read_text(encoding="utf-8")) for entry in CORPUS]


def build_spec(created_at: str) -> dict:
    """Assemble the frozen corpus description, including the QC-derived segment boundaries."""
    qc = load_qc()
    files = []
    for entry, report in zip(CORPUS, qc, strict=True):
        raw = entry.raw_path
        files.append(
            {
                "file_index": entry.file_index,
                "filename": entry.name,
                "sha256": sha256_file(raw),
                "size_bytes": raw.stat().st_size,
                "first_local_receive_ns": report["file"]["first_local_receive_ns"],
                "last_local_receive_ns": report["file"]["last_local_receive_ns"],
                "first_exchange_time_ms": report["file"]["first_exchange_time_ms"],
                "last_exchange_time_ms": report["file"]["last_exchange_time_ms"],
                "raw_records": report["file"]["raw_records"],
                "final_update_id": report["file"]["final_update_id"],
                "final_book_checksum": report["file"]["final_checksum"],
                "snapshots": report["file"]["snapshots"],
                "segments": [
                    {
                        "segment_id": segment["segment_id"],
                        "start_ns": segment["start_ns"],
                        "end_ns": segment["end_ns"],
                        "duration_s": segment["duration_s"],
                        "close_reason": segment["close_reason"],
                    }
                    for segment in report["segments"]
                ],
            }
        )
    return {
        "schema": SCHEMA,
        "corpus_id": "native_dev_v1",
        "created_at": created_at,
        "git_commit": git_commit(),
        "source": "native_binance_usdm",
        "collector_location": "aws_london",
        "symbol": "BTCUSDT",
        "venue": "Binance USD-M perpetual futures",
        "development_only": True,
        "is_out_of_sample": False,
        "notes": {
            "concatenation": "forbidden: each file is a separate collector process with its own "
            "REST snapshot",
            "segment_definition": "maximal interval with a synchronized depth book and a "
            "connected aggressive-trade stream",
            "boundary_rule": "no feature window, no forward target and no passive-fill "
            "simulation may cross a segment boundary",
            "excluded": "the rotation-enabled forward capture file and every later AWS file are "
            "not part of this corpus",
            "dataset_location": str(DATASET_DIR.relative_to(ROOT)),
        },
        "files": files,
    }


def freeze(created_at: str) -> dict:
    spec = build_spec(created_at)
    SPEC_PATH.parent.mkdir(parents=True, exist_ok=True)
    SPEC_PATH.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return spec


def verify() -> list[str]:
    """Re-hash the raw files and report any drift from the frozen spec."""
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    problems: list[str] = []
    for record in spec["files"]:
        path = RAW_DIR / record["filename"]
        if not path.exists():
            problems.append(f"missing raw file {record['filename']}")
            continue
        if path.stat().st_size != record["size_bytes"]:
            problems.append(f"size drift in {record['filename']}")
        if sha256_file(path) != record["sha256"]:
            problems.append(f"sha256 drift in {record['filename']}")
    return problems
