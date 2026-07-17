"""Immutable, data-only preparation for the authorized final-test attempt."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import polars as pl

from ashare_multifactor.config import Period, ResearchConfig, load_config
from ashare_multifactor.data.manifest import validate_panel_source
from ashare_multifactor.data.security import market_for_symbol
from ashare_multifactor.final_test.data_extension import (
    build_final_test_daily_panel,
    recover_final_test_daily_panel,
)
from ashare_multifactor.final_test.data_inventory import (
    file_identity,
    fsync_directory,
    verify_file_identity,
    write_json,
)
from ashare_multifactor.final_test.data_publication import (
    FinalTestDataResolution,
    resolve_final_test_data_panel,
)
from ashare_multifactor.final_test.gate import (
    FINAL_TEST_END,
    FINAL_TEST_START,
    FinalTestAuthorization,
    authorize_final_test,
    recover_registered_authorization,
)
from ashare_multifactor.final_test.registry import (
    append_attempt_state,
    append_attempt_outcome,
    claim_attempt_preparation,
    resolve_attempt_state,
    resolve_attempt_state_readonly,
    validate_publication_id,
)


_SCHEMA_VERSION = "1"
_SUPPORTED_MARKETS = ("sh", "sz")


@dataclass(frozen=True)
class FinalTestPreparation:
    attempt_id: str
    state: str
    root: Path
    manifest_path: Path
    manifest_sha256: str
    symbol_scope_path: Path
    symbol_count: int
    symbols_sha256: str


def prepare_final_test(
    *,
    code_root: Path,
    data_root: Path,
    opening_token_path: Path,
    approval_key: bytes,
    attempt_id: str | None = None,
) -> FinalTestPreparation:
    """Authorize, build only the final-test panel, and publish its symbol scope."""
    code_root = code_root.resolve()
    data_root = data_root.resolve()
    if attempt_id is not None:
        validate_publication_id(attempt_id)
    final_root = data_root / "processed/final_test"
    registry_root = final_root / "attempts"
    actual_attempt_id = attempt_id or uuid4().hex
    with claim_attempt_preparation(registry_root, attempt_id=actual_attempt_id):
        try:
            existing = _existing_attempt_authorization(registry_root, actual_attempt_id)
            if existing is not None:
                authorization = recover_registered_authorization(
                    code_root=code_root,
                    robustness_root=data_root / "processed/robustness",
                    validation_root=data_root / "processed/validation_evaluation",
                    opening_token_path=opening_token_path,
                    approval_key=approval_key,
                    registry_root=registry_root,
                    requested_start=FINAL_TEST_START,
                    requested_end=FINAL_TEST_END,
                    attempt_id=actual_attempt_id,
                )
                return _recover_preparation(
                    final_root,
                    registry_root=registry_root,
                    authorization=authorization,
                    code_root=code_root,
                    data_root=data_root,
                )

            authorization = authorize_final_test(
                code_root=code_root,
                robustness_root=data_root / "processed/robustness",
                validation_root=data_root / "processed/validation_evaluation",
                opening_token_path=opening_token_path,
                approval_key=approval_key,
                registry_root=registry_root,
                requested_start=FINAL_TEST_START,
                requested_end=FINAL_TEST_END,
                attempt_id=actual_attempt_id,
            )
            append_attempt_state(
                registry_root, attempt_id=authorization.attempt_id, state="preparing"
            )
            return _complete_preparation(
                final_root,
                authorization=authorization,
                code_root=code_root,
                data_root=data_root,
            )
        except Exception as error:
            _record_preparation_failure(registry_root, actual_attempt_id, error)
            raise


def _complete_preparation(
    final_root: Path,
    *,
    authorization: FinalTestAuthorization,
    code_root: Path,
    data_root: Path,
) -> FinalTestPreparation:
    registry_root = final_root / "attempts"

    config = _load_frozen_config(code_root, data_root)
    _assert_final_root(config, final_root)
    preparation_root = final_root / "preparations" / authorization.attempt_id
    _assert_new_preparation_root(preparation_root)
    resolution = _resolve_or_build_data_panel(
        config, authorization, code_root=code_root, final_root=final_root
    )
    source = validate_panel_source(
        resolution.root, Period(FINAL_TEST_START, FINAL_TEST_END)
    )
    symbols = _symbols_from_verified_panel(source)
    result = _publish_preparation(
        final_root,
        authorization=authorization,
        resolution=resolution,
        symbols=symbols,
    )
    _verify_preparation_files(
        final_root,
        preparation_root=result.root,
        authorization=authorization,
        resolution=resolution,
    )
    append_attempt_state(
        registry_root,
        attempt_id=authorization.attempt_id,
        state="awaiting_official_evidence",
        identities={"prepare_manifest_sha256": result.manifest_sha256},
    )
    return result


def _recover_preparation(
    final_root: Path,
    *,
    registry_root: Path,
    authorization: FinalTestAuthorization,
    code_root: Path,
    data_root: Path,
) -> FinalTestPreparation:
    state = resolve_attempt_state(registry_root, authorization.attempt_id)
    if state["state"] == "awaiting_official_evidence":
        return verify_preparation(
            final_root,
            attempt_id=authorization.attempt_id,
            authorization=authorization,
        )
    if state["state"] == "registered":
        append_attempt_state(
            registry_root, attempt_id=authorization.attempt_id, state="preparing"
        )
    elif state["state"] != "preparing":
        raise ValueError("final-test attempt cannot resume preparation from its state")
    root = final_root / "preparations" / authorization.attempt_id
    if not root.exists() and not root.is_symlink():
        return _complete_preparation(
            final_root,
            authorization=authorization,
            code_root=code_root,
            data_root=data_root,
        )
    resolution = resolve_final_test_data_panel(final_root)
    if resolution.claim_status != "published" or resolution.requires_recovery:
        raise ValueError("final-test data publication is not ready for preparation")
    _assert_data_claim_identity(
        final_root / "data-build-claim.json", authorization, resolution
    )
    result = _verify_preparation_files(
        final_root,
        preparation_root=root,
        authorization=authorization,
        resolution=resolution,
    )
    append_attempt_state(
        registry_root,
        attempt_id=authorization.attempt_id,
        state="awaiting_official_evidence",
        identities={"prepare_manifest_sha256": result.manifest_sha256},
    )
    return result


def verify_preparation(
    final_root: Path,
    *,
    attempt_id: str,
    authorization: FinalTestAuthorization,
    expected_state: str = "awaiting_official_evidence",
) -> FinalTestPreparation:
    """Verify exact files and identities; never rebuild or replace symbol scope."""
    if not isinstance(authorization, FinalTestAuthorization):
        raise TypeError("authorization must be a FinalTestAuthorization")
    validate_publication_id(attempt_id)
    if final_root.is_symlink():
        raise ValueError("final-test preparation root uses a symlink")
    final_root = final_root.resolve()
    if authorization.attempt_id != attempt_id:
        raise ValueError("preparation attempt differs from authorization")
    if expected_state not in {"awaiting_official_evidence", "executing"}:
        raise ValueError("invalid final-test preparation expected state")
    state = resolve_attempt_state_readonly(final_root / "attempts", attempt_id)
    if state["state"] != expected_state:
        label = expected_state.replace("_", " ")
        raise ValueError(f"final-test preparation is not {label}")
    _assert_registered_identity(state, authorization)
    resolution = resolve_final_test_data_panel(final_root)
    if resolution.claim_status != "published" or resolution.requires_recovery:
        raise ValueError("final-test data publication is not ready for preparation")
    result = _verify_preparation_files(
        final_root,
        preparation_root=final_root / "preparations" / attempt_id,
        authorization=authorization,
        resolution=resolution,
    )
    if state["identities"].get("prepare_manifest_sha256") != result.manifest_sha256:
        raise ValueError("attempt state preparation manifest identity differs")
    return replace(result, state=expected_state)


def _load_frozen_config(code_root: Path, data_root: Path) -> ResearchConfig:
    config = load_config(code_root / "configs/research_protocol.yaml")
    paths = replace(
        config.paths,
        **{
            name: path if path.is_absolute() else data_root / path
            for name, path in vars(config.paths).items()
        },
    )
    return replace(config, paths=paths)


def _existing_attempt_authorization(
    registry_root: Path, attempt_id: str | None
) -> FinalTestAuthorization | None:
    if attempt_id is None:
        return None
    record_path = registry_root / f"{attempt_id}.json"
    if not record_path.exists():
        return None
    if record_path.is_symlink():
        raise ValueError("final-test attempt record uses a symlink")
    state = resolve_attempt_state(registry_root, attempt_id)
    required = (
        "approval_id",
        "registered_at",
        "git_commit",
        "git_tree",
        "sealed_protocol_sha256",
        "robustness_release",
        "robustness_manifest_sha256",
        "robustness_lineage_sha256",
    )
    if any(not isinstance(state.get(field), str) or not state[field] for field in required):
        raise ValueError("invalid final-test attempt registration")
    return FinalTestAuthorization(
        attempt_id=attempt_id,
        approval_id=str(state["approval_id"]),
        registered_at=str(state["registered_at"]),
        git_commit=str(state["git_commit"]),
        git_tree=str(state["git_tree"]),
        sealed_protocol_sha256=str(state["sealed_protocol_sha256"]),
        robustness_release=str(state["robustness_release"]),
        robustness_manifest_sha256=str(state["robustness_manifest_sha256"]),
        robustness_lineage_sha256=str(state["robustness_lineage_sha256"]),
        test_period=(FINAL_TEST_START, FINAL_TEST_END),
    )


def _resolve_or_build_data_panel(
    config: ResearchConfig,
    authorization: FinalTestAuthorization,
    *,
    code_root: Path,
    final_root: Path,
) -> FinalTestDataResolution:
    claim_path = final_root / "data-build-claim.json"
    if claim_path.exists() or claim_path.is_symlink():
        resolution = recover_final_test_daily_panel(
            config,
            authorization,
            FINAL_TEST_START,
            FINAL_TEST_END,
            code_root=code_root,
        )
    else:
        build_final_test_daily_panel(
            config,
            authorization,
            FINAL_TEST_START,
            FINAL_TEST_END,
            code_root=code_root,
        )
        resolution = resolve_final_test_data_panel(final_root)
    if resolution.claim_status != "published" or resolution.requires_recovery:
        raise ValueError("final-test data publication is not ready for preparation")
    _assert_data_claim_identity(claim_path, authorization, resolution)
    return resolution


def _record_preparation_failure(
    registry_root: Path, attempt_id: str, error: Exception
) -> None:
    record = registry_root / f"{attempt_id}.json"
    outcome = registry_root / f"{attempt_id}.outcome.json"
    if not record.is_file() or outcome.exists():
        return
    append_attempt_outcome(
        registry_root,
        attempt_id=attempt_id,
        status="failed",
        authoritative=False,
        reason=f"{type(error).__name__}: {error}",
    )


def _assert_data_claim_identity(
    claim_path: Path,
    authorization: FinalTestAuthorization,
    resolution: FinalTestDataResolution,
) -> None:
    try:
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("invalid final-test data publication claim") from error
    expected = {
        "attempt_id": authorization.attempt_id,
        "approval_id": authorization.approval_id,
        "git_commit": authorization.git_commit,
        "git_tree": authorization.git_tree,
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "robustness_release": authorization.robustness_release,
        "robustness_manifest_sha256": authorization.robustness_manifest_sha256,
        "robustness_lineage_sha256": authorization.robustness_lineage_sha256,
        "status": "published",
    }
    if not isinstance(claim, dict) or any(
        claim.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("final-test data claim differs from authorization")
    record = claim.get("data_manifest")
    if not isinstance(record, dict) or record.get("sha256") != resolution.data_manifest_sha256:
        raise ValueError("final-test data manifest identity differs from claim")


def _symbols_from_verified_panel(source: SimpleNamespace) -> list[str]:
    frame = pl.scan_parquet(list(source.files)).select(
        pl.col("date").cast(pl.Date), pl.col("symbol").cast(pl.String).str.zfill(6)
    ).collect()
    if frame.is_empty():
        raise ValueError("final-test daily panel is empty")
    minimum = frame.get_column("date").min()
    maximum = frame.get_column("date").max()
    if (
        not isinstance(minimum, date)
        or not isinstance(maximum, date)
        or minimum < FINAL_TEST_START
        or maximum > FINAL_TEST_END
    ):
        raise ValueError("final-test panel dates are outside the sealed final-test period")
    if frame.get_column("symbol").null_count():
        raise ValueError("final-test panel contains null symbols")
    symbols = sorted(frame.get_column("symbol").unique().to_list())
    unsupported = [
        symbol for symbol in symbols if market_for_symbol(symbol) not in _SUPPORTED_MARKETS
    ]
    if unsupported:
        raise ValueError(
            "final-test panel contains unsupported market symbols: "
            + ", ".join(unsupported[:10])
        )
    return symbols


def _publish_preparation(
    final_root: Path,
    *,
    authorization: FinalTestAuthorization,
    resolution: FinalTestDataResolution,
    symbols: list[str],
) -> FinalTestPreparation:
    parent = final_root / "preparations"
    if parent.is_symlink():
        raise ValueError("final-test preparations path uses a symlink")
    parent.mkdir(parents=True, exist_ok=True)
    root = parent / authorization.attempt_id
    _assert_new_preparation_root(root)
    temporary = parent / f".{authorization.attempt_id}.{uuid4().hex}.tmp"
    temporary.mkdir()
    try:
        scope = temporary / "symbol_scope.parquet"
        pl.DataFrame({"symbol": symbols}, schema={"symbol": pl.String}).write_parquet(scope)
        scope_record = {
            **file_identity(scope, relative_path="symbol_scope.parquet"),
            "symbol_count": len(symbols),
            "symbols_sha256": _symbols_digest(symbols),
        }
        data_manifest = resolution.root / "data_manifest.json"
        manifest = {
            "schema_version": _SCHEMA_VERSION,
            "attempt_id": authorization.attempt_id,
            "approval_id": authorization.approval_id,
            "stage8": {
                "run_id": authorization.robustness_release,
                "manifest_sha256": authorization.robustness_manifest_sha256,
                "lineage_sha256": authorization.robustness_lineage_sha256,
                "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
            },
            "git": {"commit": authorization.git_commit, "tree": authorization.git_tree},
            "period": [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()],
            "supported_markets": list(_SUPPORTED_MARKETS),
            "data_manifest": file_identity(
                data_manifest, relative_path="daily_panel/data_manifest.json"
            ),
            "symbol_scope": scope_record,
            "created_at": authorization.registered_at,
            "state": "awaiting_official_evidence",
        }
        write_json(temporary / "prepare_manifest.json", manifest)
        os.replace(temporary, root)
        fsync_directory(parent)
    except BaseException:
        if temporary.exists():
            for path in temporary.iterdir():
                path.unlink()
            temporary.rmdir()
        raise
    manifest_path = root / "prepare_manifest.json"
    return FinalTestPreparation(
        attempt_id=authorization.attempt_id,
        state="awaiting_official_evidence",
        root=root,
        manifest_path=manifest_path,
        manifest_sha256=_sha256_file(manifest_path),
        symbol_scope_path=root / "symbol_scope.parquet",
        symbol_count=len(symbols),
        symbols_sha256=_symbols_digest(symbols),
    )


def _verify_preparation_files(
    final_root: Path,
    *,
    preparation_root: Path,
    authorization: FinalTestAuthorization,
    resolution: FinalTestDataResolution,
) -> FinalTestPreparation:
    if preparation_root.is_symlink() or not preparation_root.is_dir():
        raise ValueError("final-test preparation directory is missing or uses a symlink")
    if preparation_root.resolve().parent != (final_root / "preparations").resolve():
        raise ValueError("final-test preparation directory escapes final-test root")
    manifest_path = preparation_root / "prepare_manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("final-test preparation manifest is missing or uses a symlink")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("invalid final-test preparation manifest") from error
    _validate_preparation_manifest(manifest, authorization, resolution)
    data_path = verify_file_identity(final_root, manifest["data_manifest"], "data manifest")
    if data_path != resolution.root / "data_manifest.json":
        raise ValueError("preparation data manifest path is not canonical")
    scope = _verify_symbol_scope(preparation_root, manifest["symbol_scope"])
    source = validate_panel_source(
        resolution.root, Period(FINAL_TEST_START, FINAL_TEST_END)
    )
    if scope[3] != _symbols_from_verified_panel(source):
        raise ValueError("revalidated final-test panel symbol scope differs")
    return FinalTestPreparation(
        attempt_id=authorization.attempt_id,
        state="awaiting_official_evidence",
        root=preparation_root,
        manifest_path=manifest_path,
        manifest_sha256=_sha256_file(manifest_path),
        symbol_scope_path=scope[0],
        symbol_count=scope[1],
        symbols_sha256=scope[2],
    )


def _validate_preparation_manifest(
    manifest: object,
    authorization: FinalTestAuthorization,
    resolution: FinalTestDataResolution,
) -> None:
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "attempt_id",
        "approval_id",
        "stage8",
        "git",
        "period",
        "supported_markets",
        "data_manifest",
        "symbol_scope",
        "created_at",
        "state",
    }:
        raise ValueError("invalid final-test preparation manifest")
    if (
        manifest["schema_version"] != _SCHEMA_VERSION
        or manifest["attempt_id"] != authorization.attempt_id
        or manifest["approval_id"] != authorization.approval_id
        or manifest["period"]
        != [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()]
        or manifest["supported_markets"] != list(_SUPPORTED_MARKETS)
        or manifest["state"] != "awaiting_official_evidence"
        or not isinstance(manifest["created_at"], str)
        or not manifest["created_at"]
    ):
        raise ValueError("final-test preparation manifest identity differs")
    if manifest["stage8"] != {
        "run_id": authorization.robustness_release,
        "manifest_sha256": authorization.robustness_manifest_sha256,
        "lineage_sha256": authorization.robustness_lineage_sha256,
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
    } or manifest["git"] != {
        "commit": authorization.git_commit,
        "tree": authorization.git_tree,
    }:
        raise ValueError("final-test preparation manifest identity differs")
    data_manifest = manifest["data_manifest"]
    if (
        not isinstance(data_manifest, dict)
        or data_manifest.get("relative_path") != "daily_panel/data_manifest.json"
        or data_manifest.get("sha256") != resolution.data_manifest_sha256
    ):
        raise ValueError("final-test preparation data manifest identity differs")
    scope = manifest["symbol_scope"]
    if (
        not isinstance(scope, dict)
        or set(scope) != {"relative_path", "sha256", "size_bytes", "symbol_count", "symbols_sha256"}
        or scope.get("relative_path") != "symbol_scope.parquet"
        or not isinstance(scope.get("symbol_count"), int)
        or scope["symbol_count"] <= 0
        or not _is_sha256(scope.get("symbols_sha256"))
    ):
        raise ValueError("invalid final-test preparation symbol scope")


def _verify_symbol_scope(root: Path, record: object) -> tuple[Path, int, str, list[str]]:
    if not isinstance(record, dict):
        raise ValueError("invalid final-test preparation symbol scope")
    path = verify_file_identity(root, record, "symbol scope")
    try:
        frame = pl.read_parquet(path)
    except pl.exceptions.PolarsError as error:
        raise ValueError("invalid final-test preparation symbol scope") from error
    if frame.columns != ["symbol"] or frame.height == 0 or frame.null_count().item() != 0:
        raise ValueError("invalid final-test preparation symbol scope")
    symbols = frame.get_column("symbol").cast(pl.String).to_list()
    if symbols != sorted(set(symbols)):
        raise ValueError("final-test preparation symbol scope is not sorted and unique")
    if any(len(symbol) != 6 or not symbol.isdigit() for symbol in symbols):
        raise ValueError("invalid final-test preparation symbol scope")
    if any(market_for_symbol(symbol) not in _SUPPORTED_MARKETS for symbol in symbols):
        raise ValueError("final-test preparation symbol scope contains unsupported market")
    digest = _symbols_digest(symbols)
    if record["symbol_count"] != len(symbols) or record["symbols_sha256"] != digest:
        raise ValueError("final-test preparation symbol scope identity differs")
    return path, len(symbols), digest, symbols


def _assert_registered_identity(
    state: dict[str, object], authorization: FinalTestAuthorization
) -> None:
    expected = {
        "attempt_id": authorization.attempt_id,
        "approval_id": authorization.approval_id,
        "git_commit": authorization.git_commit,
        "git_tree": authorization.git_tree,
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "robustness_release": authorization.robustness_release,
        "robustness_manifest_sha256": authorization.robustness_manifest_sha256,
        "robustness_lineage_sha256": authorization.robustness_lineage_sha256,
    }
    if any(state.get(key) != value for key, value in expected.items()):
        raise ValueError("final-test preparation authorization identity differs")


def _assert_final_root(config: ResearchConfig, final_root: Path) -> None:
    expected = config.paths.processed / "final_test"
    if expected.resolve() != final_root.resolve():
        raise ValueError("configured processed path differs from final-test data root")
    if any(path.is_symlink() for path in (config.paths.processed, final_root)):
        raise ValueError("final-test preparation path uses a symlink")


def _assert_new_preparation_root(root: Path) -> None:
    if root.exists() or root.is_symlink():
        raise FileExistsError(f"final-test preparation already exists: {root}")


def _symbols_digest(symbols: list[str]) -> str:
    return hashlib.sha256(("\n".join(symbols) + "\n").encode()).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
