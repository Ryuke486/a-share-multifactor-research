from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from datetime import date
import hashlib
import json
import os
from pathlib import Path, PurePosixPath

import polars as pl

from ashare_multifactor.config import ResearchConfig
from ashare_multifactor.data.discovery import DailyFilePair
from ashare_multifactor.final_test.recovery_secure_fs import (
    opened_directory_at,
    read_bytes_at,
)


def build_input_inventory(
    config: ResearchConfig,
    start: date,
    end: date,
    *,
    discover: Callable[[Path, Path, date, date], list[DailyFilePair]],
) -> dict[str, object]:
    pairs = discover(
        config.paths.raw_unadjusted,
        config.paths.raw_backward_adjusted,
        start,
        end,
    )
    if not pairs:
        raise ValueError("no paired daily files found")
    return build_input_inventory_from_pairs(config, start, end, pairs)


def build_input_inventory_from_pairs(
    config: ResearchConfig,
    start: date,
    end: date,
    pairs: list[DailyFilePair],
) -> dict[str, object]:
    if not pairs:
        raise ValueError("no paired daily files found")
    return {
        "schema_version": "1",
        "period": [start.isoformat(), end.isoformat()],
        "pairs": [_pair_record(config, pair) for pair in pairs],
    }


def load_bound_input_pairs(
    config: ResearchConfig,
    inventory: object,
    *,
    start: date,
    end: date,
) -> list[DailyFilePair]:
    """Rebuild exact input objects from a claim-bound inventory, without discovery."""
    if (
        not isinstance(inventory, dict)
        or inventory.get("schema_version") != "1"
        or inventory.get("period") != [start.isoformat(), end.isoformat()]
        or not isinstance(inventory.get("pairs"), list)
        or not inventory["pairs"]
    ):
        raise ValueError("invalid claim-bound final-test input inventory")
    pairs: list[DailyFilePair] = []
    for item in inventory["pairs"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"trade_date", "files"}
            or not isinstance(item.get("files"), list)
            or len(item["files"]) != 2
        ):
            raise ValueError("invalid claim-bound final-test input inventory")
        try:
            trading_date = date.fromisoformat(str(item["trade_date"]))
        except ValueError as error:
            raise ValueError("invalid claim-bound final-test input inventory") from error
        files = {
            str(record.get("role")): _verify_bound_raw_file(config, record)
            for record in item["files"]
            if isinstance(record, dict)
        }
        if set(files) != {"unadjusted", "backward_adjusted"}:
            raise ValueError("invalid claim-bound final-test input inventory")
        pairs.append(
            DailyFilePair(
                trading_date=trading_date,
                unadjusted=files["unadjusted"],
                backward_adjusted=files["backward_adjusted"],
            )
        )
    if pairs != sorted(pairs, key=lambda pair: pair.trading_date) or any(
        pair.trading_date < start or pair.trading_date > end for pair in pairs
    ):
        raise ValueError("claim-bound final-test input inventory is not ordered")
    return pairs


def _verify_bound_raw_file(
    config: ResearchConfig, record: dict[str, object]
) -> Path:
    role = str(record.get("role", ""))
    root = {
        "unadjusted": config.paths.raw_unadjusted,
        "backward_adjusted": config.paths.raw_backward_adjusted,
    }.get(role)
    relative = record.get("relative_path")
    if (
        root is None
        or not isinstance(relative, str)
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        raise ValueError("invalid claim-bound final-test input file")
    path = root / relative
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("invalid claim-bound final-test input file")
    actual = _input_file_record(path, root, role=role)
    if actual != record:
        raise ValueError("claim-bound final-test input file identity changed")
    return path


def build_data_manifest(root: Path) -> dict[str, object]:
    stage2_path = root / "manifest.json"
    inventory_path = root / "input_files.json"
    stage2 = json.loads(stage2_path.read_text(encoding="utf-8"))
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    quality = stage2.get("quality_issues") if isinstance(stage2, dict) else None
    pairs = inventory.get("pairs") if isinstance(inventory, dict) else None
    if not isinstance(quality, dict) or not isinstance(pairs, list):
        raise ValueError("cannot bind invalid Stage-2 or input manifest")
    files = [
        item
        for pair in pairs
        if isinstance(pair, dict) and isinstance(pair.get("files"), list)
        for item in pair["files"]
    ]
    return {
        "schema_version": "1",
        "stage2_manifest": file_identity(
            stage2_path,
            relative_path="manifest.json",
        ),
        "input_files": {
            **file_identity(inventory_path, relative_path="input_files.json"),
            "pair_count": len(pairs),
            "file_count": len(files),
        },
        "quality_issues": dict(quality),
    }


def verify_final_test_data_panel(root: Path) -> dict[str, object]:
    """Verify the final-test identity chain without changing Stage-2 schema."""
    manifest_path = root / "data_manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("final-test data manifest is missing or uses a symlink")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("invalid final-test data manifest") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "1":
        raise ValueError("invalid final-test data manifest")

    stage2_record = _required_record(manifest, "stage2_manifest")
    inventory_record = _required_record(manifest, "input_files")
    quality_record = _required_record(manifest, "quality_issues")
    stage2_path = verify_file_identity(root, stage2_record, "Stage-2 manifest")
    inventory_path = verify_file_identity(root, inventory_record, "input files")
    verify_file_identity(root, quality_record, "quality issues")

    try:
        stage2 = json.loads(stage2_path.read_text(encoding="utf-8"))
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("invalid bound final-test data input") from error
    if not isinstance(stage2, dict) or stage2.get("quality_issues") != quality_record:
        raise ValueError("quality issues identity differs from Stage-2 manifest")
    pairs = inventory.get("pairs") if isinstance(inventory, dict) else None
    if not isinstance(pairs, list):
        raise ValueError("invalid final-test input file inventory")
    file_count = sum(
        len(pair.get("files", []))
        for pair in pairs
        if isinstance(pair, dict) and isinstance(pair.get("files"), list)
    )
    if (
        inventory_record.get("pair_count") != len(pairs)
        or inventory_record.get("file_count") != file_count
        or file_count != 2 * len(pairs)
    ):
        raise ValueError("final-test input file inventory count mismatch")
    return manifest


def verify_final_test_data_panel_at(root_fd: int) -> dict[str, object]:
    """Verify a final-test panel entirely below one held directory descriptor."""
    try:
        manifest = json.loads(
            read_bytes_at(
                root_fd,
                "data_manifest.json",
                label="final-test data manifest",
            )
        )
    except json.JSONDecodeError as error:
        raise ValueError("invalid final-test data manifest") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "1":
        raise ValueError("invalid final-test data manifest")

    stage2_record = _required_record(manifest, "stage2_manifest")
    inventory_record = _required_record(manifest, "input_files")
    quality_record = _required_record(manifest, "quality_issues")
    stage2_bytes = verify_file_identity_at(root_fd, stage2_record, "Stage-2 manifest")
    inventory_bytes = verify_file_identity_at(root_fd, inventory_record, "input files")
    verify_file_identity_at(root_fd, quality_record, "quality issues")
    try:
        stage2 = json.loads(stage2_bytes)
        inventory = json.loads(inventory_bytes)
    except json.JSONDecodeError as error:
        raise ValueError("invalid bound final-test data input") from error
    if not isinstance(stage2, dict) or stage2.get("quality_issues") != quality_record:
        raise ValueError("quality issues identity differs from Stage-2 manifest")
    pairs = inventory.get("pairs") if isinstance(inventory, dict) else None
    if not isinstance(pairs, list):
        raise ValueError("invalid final-test input file inventory")
    file_count = sum(
        len(pair.get("files", []))
        for pair in pairs
        if isinstance(pair, dict) and isinstance(pair.get("files"), list)
    )
    if (
        inventory_record.get("pair_count") != len(pairs)
        or inventory_record.get("file_count") != file_count
        or file_count != 2 * len(pairs)
    ):
        raise ValueError("final-test input file inventory count mismatch")
    return manifest


def verify_file_identity_at(
    root_fd: int,
    record: dict[str, object],
    label: str,
) -> bytes:
    """Read and verify one relative file without reopening its named root."""
    relative = record.get("relative_path")
    if not isinstance(relative, str):
        raise ValueError(f"invalid {label} identity path")
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts or "." in path.parts:
        raise ValueError(f"invalid {label} identity path")
    with ExitStack() as stack:
        parent_fd = root_fd
        for part in path.parts[:-1]:
            parent_fd = stack.enter_context(
                opened_directory_at(parent_fd, part, label=f"{label} parent")
            )
        try:
            payload = read_bytes_at(parent_fd, path.name, label=label)
        except FileNotFoundError as error:
            raise ValueError(f"{label} identity mismatch") from error
    if (
        record.get("sha256") != hashlib.sha256(payload).hexdigest()
        or record.get("size_bytes") != len(payload)
    ):
        raise ValueError(f"{label} identity mismatch")
    return payload


def verify_file_identity(root: Path, record: dict[str, object], label: str) -> Path:
    relative = record.get("relative_path")
    if (
        not isinstance(relative, str)
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        raise ValueError(f"invalid {label} identity path")
    path = root / relative
    if (
        path.is_symlink()
        or not path.is_file()
        or not path.resolve().is_relative_to(root.resolve())
    ):
        raise ValueError(f"invalid {label} identity path")
    actual = file_identity(path, relative_path=relative)
    if (
        record.get("sha256") != actual["sha256"]
        or record.get("size_bytes") != actual["size_bytes"]
    ):
        raise ValueError(f"{label} identity mismatch")
    return path


def file_identity(path: Path, *, relative_path: str) -> dict[str, object]:
    return {
        "relative_path": relative_path,
        "sha256": _file_sha256(path),
        "size_bytes": path.stat().st_size,
    }


def write_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _required_record(
    payload: dict[str, object],
    key: str,
) -> dict[str, object]:
    record = payload.get(key)
    if not isinstance(record, dict):
        raise ValueError(f"final-test data manifest lacks {key} identity")
    return record


def _pair_record(config: ResearchConfig, pair: DailyFilePair) -> dict[str, object]:
    return {
        "trade_date": pair.trading_date.isoformat(),
        "files": [
            _input_file_record(
                pair.unadjusted,
                config.paths.raw_unadjusted,
                role="unadjusted",
            ),
            _input_file_record(
                pair.backward_adjusted,
                config.paths.raw_backward_adjusted,
                role="backward_adjusted",
            ),
        ],
    }


def _input_file_record(path: Path, root: Path, *, role: str) -> dict[str, object]:
    return {
        "role": role,
        "relative_path": path.relative_to(root).as_posix(),
        "sha256": _file_sha256(path),
        "size_bytes": path.stat().st_size,
        "rows": pl.scan_csv(path).select(pl.len()).collect().item(),
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
