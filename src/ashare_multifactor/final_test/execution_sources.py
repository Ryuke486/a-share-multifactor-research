from __future__ import annotations

from collections.abc import Mapping
from io import BytesIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import polars as pl

import json
import hashlib

from ashare_multifactor.audit.records import file_record, sha256_file, verify_file_record
from ashare_multifactor.audit.secure_tree import (
    atomic_rename_no_replace_at,
    frozen_records,
    read_frozen_tree_at,
    write_frozen_tree_at,
)
from ashare_multifactor.data.security import market_for_symbol
from ashare_multifactor.execution.corporate_actions import normalize_corporate_actions
from ashare_multifactor.final_test.action_source_contract import (
    OFFICIAL_MARKET_SOURCES,
    evidence_url_matches_source,
    load_action_source_contract,
)
from ashare_multifactor.final_test.corporate_action_coverage import (
    validate_corporate_action_coverage,
)
from ashare_multifactor.final_test.data_publication import (
    resolve_final_test_data_panel,
    resolve_final_test_data_panel_at,
)
from ashare_multifactor.final_test.execution_contracts import (
    normalize_security_event_rows,
)
from ashare_multifactor.final_test.execution_identity import (
    assert_execution_identity_authorized,
)
from ashare_multifactor.final_test.gate import (
    FINAL_TEST_END,
    FINAL_TEST_START,
    FinalTestAuthorization,
)
from ashare_multifactor.final_test.final_root_binding import FinalRootBinding
from ashare_multifactor.final_test.official_query_coverage import OfficialQueryScope
from ashare_multifactor.final_test.official_query_index import (
    validate_official_query_coverage_index,
)
from ashare_multifactor.final_test.recovery_secure_fs import (
    assert_directory_entry,
    directory_identity,
    opened_directory,
    opened_directory_at,
)


def validate_final_execution_coverages(
    *,
    symbols: list[str],
    security_event_coverage_path: Path,
    corporate_action_coverage_root: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return both coverages with hashes bound by their own validators."""
    return (
        validate_security_event_coverage(
            security_event_coverage_path,
            symbols=symbols,
        ),
        validate_corporate_action_coverage(
            corporate_action_coverage_root,
            symbols=symbols,
        ),
    )


def freeze_final_execution_parquets(
    *,
    symbols: list[str],
    security_event_coverage_path: Path,
    corporate_action_coverage_root: Path,
    execution_identity: Mapping[str, object],
    coverage_snapshot_files: Mapping[str, bytes] | None = None,
) -> dict[str, bytes]:
    """Derive deterministic primary bytes only from identity-bound coverages."""
    if coverage_snapshot_files is None:
        security, corporate = validate_final_execution_coverages(
            symbols=symbols,
            security_event_coverage_path=security_event_coverage_path,
            corporate_action_coverage_root=corporate_action_coverage_root,
        )
    else:
        security, corporate, _security_files, _corporate_files = (
            _validated_coverages_from_snapshot(
                coverage_snapshot_files,
                symbols=symbols,
                expected_security_sha256=str(
                    execution_identity["security_event_coverage_sha256"]
                ),
                expected_corporate_sha256=str(
                    execution_identity["corporate_action_coverage_sha256"]
                ),
            )
        )
    _assert_coverage_identity(
        security,
        corporate,
        expected_security_event_coverage_sha256=str(
            execution_identity["security_event_coverage_sha256"]
        ),
        expected_corporate_action_coverage_sha256=str(
            execution_identity["corporate_action_coverage_sha256"]
        ),
    )
    actions, events = _execution_frames(security, corporate)
    return {
        "corporate_actions.parquet": _parquet_bytes(actions),
        "security_events.parquet": _parquet_bytes(events),
    }


def build_final_execution_inputs(
    authorization: FinalTestAuthorization,
    *,
    code_root: Path,
    data_root: Path,
    final_root: Path,
    security_event_coverage_path: Path,
    corporate_action_coverage_root: Path,
    symbols: list[str],
    execution_identity: Mapping[str, object],
    expected_parquet_bytes: Mapping[str, bytes] | None = None,
    root_binding: FinalRootBinding | None = None,
) -> dict[str, object]:
    """Generate, then bind, the frozen action/event inputs to this attempt."""
    del data_root
    if root_binding is not None:
        root_binding.assert_bound()
    identity = assert_execution_identity_authorized(execution_identity, authorization)
    if root_binding is not None:
        return _build_final_execution_inputs_at(
            authorization,
            code_root=code_root,
            final_root=final_root,
            symbols=symbols,
            identity=identity,
            expected_parquet_bytes=expected_parquet_bytes,
            root_binding=root_binding,
        )
    prepare_manifest_sha256 = identity["prepare_manifest_sha256"]
    expected_security_event_coverage_sha256 = identity["security_event_coverage_sha256"]
    expected_corporate_action_coverage_sha256 = identity["corporate_action_coverage_sha256"]
    execution_id = identity["execution_id"]
    coverage_snapshot_manifest_sha256 = identity["coverage_snapshot_manifest_sha256"]
    source_root = final_root / "execution_input_sources"
    execution_root = final_root / "attempt_inputs" / authorization.attempt_id
    if (
        source_root.is_symlink()
        or execution_root.is_symlink()
        or source_root.resolve() != final_root.resolve() / "execution_input_sources"
        or execution_root.resolve()
        != final_root.resolve() / "attempt_inputs" / authorization.attempt_id
    ):
        raise ValueError("final execution input path uses a symlink or escapes root")
    security_coverage, corporate_coverage = validate_final_execution_coverages(
        symbols=symbols,
        security_event_coverage_path=security_event_coverage_path,
        corporate_action_coverage_root=corporate_action_coverage_root,
    )
    _assert_coverage_identity(
        security_coverage,
        corporate_coverage,
        expected_security_event_coverage_sha256=(expected_security_event_coverage_sha256),
        expected_corporate_action_coverage_sha256=(expected_corporate_action_coverage_sha256),
    )
    if not source_root.exists():
        generate_final_execution_sources(
            authorization,
            code_root=code_root,
            final_root=final_root,
            security_event_coverage_path=security_event_coverage_path,
            corporate_action_coverage_root=corporate_action_coverage_root,
            symbols=symbols,
            prepare_manifest_sha256=prepare_manifest_sha256,
            expected_security_event_coverage_sha256=(expected_security_event_coverage_sha256),
            expected_corporate_action_coverage_sha256=(expected_corporate_action_coverage_sha256),
            execution_id=execution_id,
            coverage_snapshot_manifest_sha256=(coverage_snapshot_manifest_sha256),
            final_fd=(root_binding.final_fd if root_binding is not None else None),
        )
    source_records = _verify_reusable_source_root(
        source_root,
        authorization=authorization,
        code_root=code_root,
        final_root=final_root,
        symbols=symbols,
        prepare_manifest_sha256=prepare_manifest_sha256,
        expected_security_event_coverage_sha256=(expected_security_event_coverage_sha256),
        expected_corporate_action_coverage_sha256=(expected_corporate_action_coverage_sha256),
        execution_id=execution_id,
        coverage_snapshot_manifest_sha256=coverage_snapshot_manifest_sha256,
        expected_security_coverage=security_coverage,
        expected_corporate_coverage=corporate_coverage,
    )
    actions, events = _execution_frames(security_coverage, corporate_coverage)
    generated_parquets = {
        "corporate_actions.parquet": _parquet_bytes(actions),
        "security_events.parquet": _parquet_bytes(events),
    }
    if expected_parquet_bytes is not None and generated_parquets != dict(expected_parquet_bytes):
        raise ValueError("deterministic execution output differs from bound intent")
    if execution_root.exists() or execution_root.is_symlink():
        raise FileExistsError("final execution inputs are already bound")
    from ashare_multifactor.final_test.coverage_snapshot import (
        freeze_bound_coverage_snapshot,
    )

    snapshot_files = freeze_bound_coverage_snapshot(
        final_root,
        attempt_id=authorization.attempt_id,
        expected_manifest_sha256=coverage_snapshot_manifest_sha256,
    )
    snapshot_records = frozen_records(
        snapshot_files,
        prefix="coverage_snapshot",
        role="official_coverage_snapshot",
    )
    manifest = _execution_input_manifest_from_bytes(
        authorization=authorization,
        files=generated_parquets,
        execution_identity=identity,
    )
    manifest["source_contract"] = file_record(
        code_root / "configs/final_execution_sources.yaml",
        root=code_root,
        role="final_execution_source_contract",
    ).to_dict()
    manifest["source_files"] = source_records
    manifest["coverage_snapshot_files"] = snapshot_records
    manifest["symbols_sha256"] = hashlib.sha256(("\n".join(symbols) + "\n").encode()).hexdigest()
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    staged_files = {
        **generated_parquets,
        **{f"coverage_snapshot/{name}": payload for name, payload in snapshot_files.items()},
        "manifest.json": manifest_bytes,
    }
    if root_binding is None:
        with opened_directory(final_root, label="final-test root") as final_fd:
            _publish_execution_inputs_at(
                final_fd,
                authorization=authorization,
                execution_id=execution_id,
                staged_files=staged_files,
            )
    else:
        _publish_execution_inputs_at(
            root_binding.final_fd,
            authorization=authorization,
            execution_id=execution_id,
            staged_files=staged_files,
        )
        root_binding.assert_bound()
    return manifest


def _build_final_execution_inputs_at(
    authorization: FinalTestAuthorization,
    *,
    code_root: Path,
    final_root: Path,
    symbols: list[str],
    identity: Mapping[str, object],
    expected_parquet_bytes: Mapping[str, bytes] | None,
    root_binding: FinalRootBinding,
) -> dict[str, object]:
    from ashare_multifactor.final_test.coverage_snapshot import (
        freeze_bound_coverage_snapshot_at,
    )

    snapshot_files = freeze_bound_coverage_snapshot_at(
        root_binding.final_fd,
        attempt_id=authorization.attempt_id,
        expected_manifest_sha256=str(identity["coverage_snapshot_manifest_sha256"]),
    )
    security, corporate, security_files, corporate_files = (
        _validated_coverages_from_snapshot(
            snapshot_files,
            symbols=symbols,
            expected_security_sha256=str(identity["security_event_coverage_sha256"]),
            expected_corporate_sha256=str(identity["corporate_action_coverage_sha256"]),
        )
    )
    resolution = resolve_final_test_data_panel_at(final_root, root_binding.final_fd)
    if resolution.claim_status != "published" or resolution.requires_recovery:
        raise ValueError("final-test data publication requires recovery")
    contract = load_action_source_contract(
        code_root / "configs/final_execution_sources.yaml"
    )
    if (
        not symbols
        or symbols != sorted(set(symbols))
        or any(
            market_for_symbol(symbol) not in contract.supported_markets
            for symbol in symbols
        )
    ):
        raise ValueError("final execution input symbols differ from preparation scope")
    actions, events = _execution_frames(security, corporate)
    generated_parquets = {
        "corporate_actions.parquet": _parquet_bytes(actions),
        "security_events.parquet": _parquet_bytes(events),
    }
    if expected_parquet_bytes is not None and generated_parquets != dict(
        expected_parquet_bytes
    ):
        raise ValueError("deterministic execution output differs from bound intent")
    complete_sources, source_records = _execution_source_tree(
        authorization=authorization,
        contract=contract,
        data_manifest_sha256=resolution.data_manifest_sha256,
        symbols=symbols,
        identity=identity,
        security_coverage=security,
        corporate_coverage=corporate,
        security_files=security_files,
        corporate_files=corporate_files,
        generated_parquets=generated_parquets,
    )
    try:
        with opened_directory_at(
            root_binding.final_fd,
            "execution_input_sources",
            label="final execution source inputs",
        ) as sources_fd:
            existing_sources = read_frozen_tree_at(
                sources_fd,
                label="final execution source inputs",
            )
    except FileNotFoundError:
        _publish_execution_sources_at(
            root_binding.final_fd,
            destination_name="execution_input_sources",
            complete_files=complete_sources,
        )
    else:
        if existing_sources != complete_sources:
            raise ValueError("reusable final execution source inventory differs")

    if _attempt_input_exists_at(
        root_binding.final_fd,
        attempt_id=authorization.attempt_id,
    ):
        raise FileExistsError("final execution inputs are already bound")
    snapshot_records = frozen_records(
        snapshot_files,
        prefix="coverage_snapshot",
        role="official_coverage_snapshot",
    )
    manifest = _execution_input_manifest_from_bytes(
        authorization=authorization,
        files=generated_parquets,
        execution_identity=identity,
    )
    manifest["source_contract"] = file_record(
        code_root / "configs/final_execution_sources.yaml",
        root=code_root,
        role="final_execution_source_contract",
    ).to_dict()
    manifest["source_files"] = source_records
    manifest["coverage_snapshot_files"] = snapshot_records
    manifest["symbols_sha256"] = hashlib.sha256(
        ("\n".join(symbols) + "\n").encode()
    ).hexdigest()
    staged_files = {
        **generated_parquets,
        **{
            f"coverage_snapshot/{name}": payload
            for name, payload in snapshot_files.items()
        },
        "manifest.json": (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8"),
    }
    _publish_execution_inputs_at(
        root_binding.final_fd,
        authorization=authorization,
        execution_id=str(identity["execution_id"]),
        staged_files=staged_files,
    )
    root_binding.assert_bound()
    return manifest


def _attempt_input_exists_at(final_fd: int, *, attempt_id: str) -> bool:
    try:
        with opened_directory_at(
            final_fd,
            "attempt_inputs",
            label="attempt-input root",
        ) as parent_fd:
            os.stat(attempt_id, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _publish_execution_inputs_at(
    final_fd: int,
    *,
    authorization: FinalTestAuthorization,
    execution_id: str,
    staged_files: Mapping[str, bytes],
) -> None:
    try:
        os.mkdir("attempt_inputs", mode=0o700, dir_fd=final_fd)
    except FileExistsError:
        pass
    with opened_directory_at(final_fd, "attempt_inputs", label="attempt-input root") as parent_fd:
        staging_name = f".{authorization.attempt_id}.{execution_id}.building"
        write_frozen_tree_at(
            parent_fd,
            staging_name,
            staged_files,
            resumable=True,
            label="execution-input staging",
        )
        with opened_directory_at(
            parent_fd, staging_name, label="execution-input staging"
        ) as staging_fd:
            staging_identity = directory_identity(staging_fd)
            if read_frozen_tree_at(staging_fd, label="execution-input staging") != staged_files:
                raise ValueError("final execution input staging inventory differs")
            atomic_rename_no_replace_at(
                parent_fd,
                staging_name,
                authorization.attempt_id,
            )
            assert_directory_entry(
                parent_fd,
                authorization.attempt_id,
                expected=staging_identity,
                label="final execution inputs",
            )


def _execution_input_manifest_from_bytes(
    *,
    authorization: FinalTestAuthorization,
    files: Mapping[str, bytes],
    execution_identity: Mapping[str, object],
) -> dict[str, object]:
    required = {"corporate_actions.parquet", "security_events.parquet"}
    if set(files) != required:
        raise ValueError("execution-input files differ from the frozen contract")
    return {
        "attempt_id": authorization.attempt_id,
        "approval_id": authorization.approval_id,
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "robustness_release": authorization.robustness_release,
        "period": [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()],
        "files": [
            {
                "path": name,
                "role": name.removesuffix(".parquet"),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
            for name, payload in sorted(files.items())
        ],
        **assert_execution_identity_authorized(execution_identity, authorization),
    }


def _write_resumable_bytes(path: Path, payload: bytes) -> None:
    """Complete only an exact prefix left by a hard interruption."""
    if path.is_symlink():
        raise ValueError("execution-input staging file uses a symlink")
    if path.exists():
        if not path.is_file():
            raise ValueError("execution-input staging contains a non-file")
        existing = path.read_bytes()
        if not payload.startswith(existing):
            raise ValueError("execution-input staging bytes differ")
        if existing == payload:
            return
        mode = "ab"
        remainder = payload[len(existing) :]
    else:
        mode = "xb"
        remainder = payload
    with path.open(mode) as stream:
        stream.write(remainder)
        stream.flush()
        os.fsync(stream.fileno())


def _parquet_bytes(frame: pl.DataFrame) -> bytes:
    buffer = BytesIO()
    frame.write_parquet(buffer)
    return buffer.getvalue()


def _validated_coverages_from_snapshot(
    snapshot_files: Mapping[str, bytes],
    *,
    symbols: list[str],
    expected_security_sha256: str,
    expected_corporate_sha256: str,
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, bytes],
    dict[str, bytes],
]:
    try:
        snapshot_manifest = json.loads(snapshot_files["snapshot_manifest.json"])
        security_relative = Path(str(snapshot_manifest["security_manifest"]))
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("invalid execution coverage snapshot manifest") from error
    if (
        not isinstance(snapshot_manifest, dict)
        or security_relative.is_absolute()
        or not security_relative.parts
        or security_relative.parts[0] != "security"
        or ".." in security_relative.parts
    ):
        raise ValueError("invalid execution coverage snapshot manifest")
    with TemporaryDirectory(prefix="bound-execution-coverage-") as scratch_name:
        scratch = Path(scratch_name).resolve()
        with opened_directory(scratch, label="bound execution coverage scratch") as scratch_fd:
            write_frozen_tree_at(
                scratch_fd,
                "snapshot",
                snapshot_files,
                resumable=False,
                label="bound execution coverage",
            )
        snapshot_root = scratch / "snapshot"
        security, corporate = validate_final_execution_coverages(
            symbols=symbols,
            security_event_coverage_path=snapshot_root / security_relative,
            corporate_action_coverage_root=snapshot_root / "corporate",
        )
        _assert_coverage_identity(
            security,
            corporate,
            expected_security_event_coverage_sha256=expected_security_sha256,
            expected_corporate_action_coverage_sha256=expected_corporate_sha256,
        )
        security_files = _freeze_verified_coverage_root(
            security,
            label="security coverage",
        )
        security_root = security.get("coverage_root")
        security_manifest = security.get("coverage_manifest_path")
        if not isinstance(security_root, Path) or not isinstance(
            security_manifest, Path
        ):
            raise ValueError("verified security coverage root is invalid")
        manifest_relative = security_manifest.relative_to(security_root).as_posix()
        manifest_bytes = security_files.pop(manifest_relative)
        existing_manifest = security_files.get("security_event_coverage.json")
        if existing_manifest is not None and existing_manifest != manifest_bytes:
            raise ValueError("security-event support file path collision")
        security_files["security_event_coverage.json"] = manifest_bytes
        corporate_files = _freeze_verified_coverage_root(
            corporate,
            label="corporate coverage",
        )
    return security, corporate, security_files, corporate_files


def _execution_source_tree(
    *,
    authorization: FinalTestAuthorization,
    contract: object,
    data_manifest_sha256: str,
    symbols: list[str],
    identity: Mapping[str, object],
    security_coverage: dict[str, object],
    corporate_coverage: dict[str, object],
    security_files: Mapping[str, bytes],
    corporate_files: Mapping[str, bytes],
    generated_parquets: Mapping[str, bytes],
) -> tuple[dict[str, bytes], list[dict[str, object]]]:
    if set(generated_parquets) & set(security_files):
        raise ValueError("security-event support file path collision")
    source_files = {
        **generated_parquets,
        **security_files,
        **{
            f"corporate_action_coverage/{name}": payload
            for name, payload in corporate_files.items()
        },
    }
    records = frozen_records(
        source_files,
        prefix="",
        role="final_execution_source",
    )
    records.sort(key=lambda record: Path(str(record["path"])))
    _actions, events = _execution_frames(security_coverage, corporate_coverage)
    source_manifest = {
        "attempt_id": authorization.attempt_id,
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "git_commit": authorization.git_commit,
        "git_tree": authorization.git_tree,
        "data_manifest_sha256": data_manifest_sha256,
        "execution_id": identity["execution_id"],
        "coverage_snapshot_manifest_sha256": identity[
            "coverage_snapshot_manifest_sha256"
        ],
        "prepare_manifest_sha256": identity["prepare_manifest_sha256"],
        "security_event_coverage_sha256": identity[
            "security_event_coverage_sha256"
        ],
        "corporate_action_coverage_sha256": identity[
            "corporate_action_coverage_sha256"
        ],
        "symbols_sha256": hashlib.sha256(
            ("\n".join(symbols) + "\n").encode()
        ).hexdigest(),
        "period": [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()],
        "provider": contract.provider,
        "query_year_type": contract.query_year_type,
        "query_years": list(contract.query_years),
        "symbol_count": len(symbols),
        "successful_query_count": corporate_coverage["coverage"].height,
        "security_event_source": (
            "official_events" if events.height else "official_zero_event_coverage"
        ),
        "files": records,
    }
    complete_files = {
        **source_files,
        "source_manifest.json": (
            json.dumps(
                source_manifest,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
    }
    return complete_files, records


def generate_final_execution_sources(
    authorization: FinalTestAuthorization,
    *,
    code_root: Path,
    final_root: Path,
    security_event_coverage_path: Path,
    corporate_action_coverage_root: Path,
    symbols: list[str],
    prepare_manifest_sha256: str,
    expected_security_event_coverage_sha256: str,
    expected_corporate_action_coverage_sha256: str,
    execution_id: str,
    coverage_snapshot_manifest_sha256: str,
    final_fd: int | None = None,
) -> Path:
    """Bind verified official action/event evidence to canonical execution inputs."""
    _assert_authorization(authorization)
    contract = load_action_source_contract(code_root / "configs/final_execution_sources.yaml")
    resolution = resolve_final_test_data_panel(final_root)
    if resolution.claim_status != "published" or resolution.requires_recovery:
        raise ValueError("final-test data publication requires recovery")
    if (
        not symbols
        or symbols != sorted(set(symbols))
        or any(market_for_symbol(symbol) not in contract.supported_markets for symbol in symbols)
    ):
        raise ValueError("final execution input symbols differ from preparation scope")
    security_coverage = validate_security_event_coverage(
        security_event_coverage_path,
        symbols=symbols,
    )
    corporate_coverage = validate_corporate_action_coverage(
        corporate_action_coverage_root,
        symbols=symbols,
    )
    _assert_coverage_identity(
        security_coverage,
        corporate_coverage,
        expected_security_event_coverage_sha256=(expected_security_event_coverage_sha256),
        expected_corporate_action_coverage_sha256=(expected_corporate_action_coverage_sha256),
    )
    actions, events = _execution_frames(security_coverage, corporate_coverage)
    destination = final_root / "execution_input_sources"
    security_files = _freeze_verified_coverage_root(security_coverage, label="security coverage")
    security_root = security_coverage.get("coverage_root")
    security_manifest = security_coverage.get("coverage_manifest_path")
    if not isinstance(security_root, Path) or not isinstance(security_manifest, Path):
        raise ValueError("verified security coverage root is invalid")
    manifest_relative = security_manifest.relative_to(security_root).as_posix()
    manifest_bytes = security_files.pop(manifest_relative)
    existing_manifest = security_files.get("security_event_coverage.json")
    if existing_manifest is not None and existing_manifest != manifest_bytes:
        raise ValueError("security-event support file path collision")
    security_files["security_event_coverage.json"] = manifest_bytes
    corporate_files = _freeze_verified_coverage_root(corporate_coverage, label="corporate coverage")
    primary_files = {
        "corporate_actions.parquet": _parquet_bytes(actions),
        "security_events.parquet": _parquet_bytes(events),
    }
    if set(primary_files) & set(security_files):
        raise ValueError("security-event support file path collision")
    source_files = {
        **primary_files,
        **security_files,
        **{
            f"corporate_action_coverage/{name}": payload
            for name, payload in corporate_files.items()
        },
    }
    records = frozen_records(
        source_files,
        prefix="",
        role="final_execution_source",
    )
    records.sort(key=lambda record: Path(str(record["path"])))
    source_manifest = {
        "attempt_id": authorization.attempt_id,
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "git_commit": authorization.git_commit,
        "git_tree": authorization.git_tree,
        "data_manifest_sha256": resolution.data_manifest_sha256,
        "execution_id": execution_id,
        "coverage_snapshot_manifest_sha256": (coverage_snapshot_manifest_sha256),
        "prepare_manifest_sha256": prepare_manifest_sha256,
        "security_event_coverage_sha256": (expected_security_event_coverage_sha256),
        "corporate_action_coverage_sha256": (expected_corporate_action_coverage_sha256),
        "symbols_sha256": hashlib.sha256(("\n".join(symbols) + "\n").encode()).hexdigest(),
        "period": [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()],
        "provider": contract.provider,
        "query_year_type": contract.query_year_type,
        "query_years": list(contract.query_years),
        "symbol_count": len(symbols),
        "successful_query_count": corporate_coverage["coverage"].height,
        "security_event_source": (
            "official_events" if events.height else "official_zero_event_coverage"
        ),
        "files": records,
    }
    complete_files = {
        **source_files,
        "source_manifest.json": (
            json.dumps(
                source_manifest,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
    }
    if final_fd is None:
        with opened_directory(final_root.parent, label="final-test parent") as final_parent_fd:
            with opened_directory_at(
                final_parent_fd, final_root.name, label="final-test root"
            ) as opened_final_fd:
                _publish_execution_sources_at(
                    opened_final_fd,
                    destination_name=destination.name,
                    complete_files=complete_files,
                )
    else:
        _publish_execution_sources_at(
            final_fd,
            destination_name=destination.name,
            complete_files=complete_files,
        )
    return destination


def _publish_execution_sources_at(
    final_fd: int,
    *,
    destination_name: str,
    complete_files: Mapping[str, bytes],
) -> None:
    temporary_name = f".execution-input-sources-{uuid4().hex}.tmp"
    write_frozen_tree_at(
        final_fd,
        temporary_name,
        complete_files,
        resumable=False,
        label="execution source staging",
    )
    with opened_directory_at(
        final_fd, temporary_name, label="execution source staging"
    ) as temporary_fd:
        temporary_identity = directory_identity(temporary_fd)
        if read_frozen_tree_at(temporary_fd, label="execution source staging") != complete_files:
            raise ValueError("execution source staging bytes differ")
        atomic_rename_no_replace_at(final_fd, temporary_name, destination_name)
        assert_directory_entry(
            final_fd,
            destination_name,
            expected=temporary_identity,
            label="final execution source inputs",
        )


def _freeze_verified_coverage_root(
    coverage: Mapping[str, object],
    *,
    label: str,
) -> dict[str, bytes]:
    root = coverage.get("coverage_root")
    if not isinstance(root, Path):
        raise ValueError(f"verified {label} root is invalid")
    with opened_directory(root, label=label) as root_fd:
        return read_frozen_tree_at(root_fd, label=label)


def _assert_coverage_identity(
    security_coverage: dict[str, object],
    corporate_coverage: dict[str, object],
    *,
    expected_security_event_coverage_sha256: str,
    expected_corporate_action_coverage_sha256: str,
) -> None:
    if (
        security_coverage.get("coverage_manifest_sha256") != expected_security_event_coverage_sha256
        or corporate_coverage.get("coverage_manifest_sha256")
        != expected_corporate_action_coverage_sha256
    ):
        raise ValueError("execution coverage snapshot differs from claimed identity")


def _execution_frames(
    security_coverage: dict[str, object],
    corporate_coverage: dict[str, object],
) -> tuple[pl.DataFrame, pl.DataFrame]:
    actions = corporate_coverage.get("actions")
    if not isinstance(actions, pl.DataFrame):
        raise ValueError("official corporate actions are not normalized")
    event_file = security_coverage.get("events_file")
    events = (
        security_coverage.get("events")
        if isinstance(event_file, Path)
        else pl.DataFrame(
            schema={
                "effective_date": pl.Date,
                "source_symbol": pl.String,
                "event_type": pl.String,
                "target_symbol": pl.String,
                "ratio": pl.Float64,
                "cash_per_share": pl.Float64,
                "source": pl.String,
                "evidence_id": pl.String,
            }
        )
    )
    if not isinstance(events, pl.DataFrame):
        raise ValueError("official security events are not normalized")
    if events.height != security_coverage.get("event_rows"):
        raise ValueError("official security-event row count changed")
    return actions, events


def _verify_reusable_source_root(
    root: Path,
    *,
    authorization: FinalTestAuthorization,
    code_root: Path,
    final_root: Path,
    symbols: list[str],
    prepare_manifest_sha256: str,
    expected_security_event_coverage_sha256: str,
    expected_corporate_action_coverage_sha256: str,
    execution_id: str,
    coverage_snapshot_manifest_sha256: str,
    expected_security_coverage: dict[str, object],
    expected_corporate_coverage: dict[str, object],
) -> list[dict[str, object]]:
    if (
        root.is_symlink()
        or not root.is_dir()
        or root.resolve() != final_root.resolve() / "execution_input_sources"
    ):
        raise ValueError("reusable final execution source path is unsafe")
    manifest_path = root / "source_manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("invalid reusable final execution source manifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid reusable final execution source manifest") from error
    resolution = resolve_final_test_data_panel(final_root)
    contract = load_action_source_contract(code_root / "configs/final_execution_sources.yaml")
    _assert_coverage_identity(
        expected_security_coverage,
        expected_corporate_coverage,
        expected_security_event_coverage_sha256=(expected_security_event_coverage_sha256),
        expected_corporate_action_coverage_sha256=(expected_corporate_action_coverage_sha256),
    )
    expected_actions, expected_events = _execution_frames(
        expected_security_coverage,
        expected_corporate_coverage,
    )
    security_coverage = validate_security_event_coverage(
        root / "security_event_coverage.json", symbols=symbols
    )
    corporate_coverage = validate_corporate_action_coverage(
        root / "corporate_action_coverage", symbols=symbols
    )
    _assert_coverage_identity(
        security_coverage,
        corporate_coverage,
        expected_security_event_coverage_sha256=(expected_security_event_coverage_sha256),
        expected_corporate_action_coverage_sha256=(expected_corporate_action_coverage_sha256),
    )
    source_actions, source_events = _execution_frames(
        security_coverage,
        corporate_coverage,
    )
    stored_actions = _read_source_frame(root / "corporate_actions.parquet")
    stored_events = _read_source_frame(root / "security_events.parquet")
    if (
        not stored_actions.equals(expected_actions)
        or not stored_actions.equals(source_actions)
        or not stored_events.equals(expected_events)
        or not stored_events.equals(source_events)
    ):
        raise ValueError("reusable final execution source data differ from coverage")
    expected = {
        "attempt_id": authorization.attempt_id,
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "git_commit": authorization.git_commit,
        "git_tree": authorization.git_tree,
        "data_manifest_sha256": resolution.data_manifest_sha256,
        "execution_id": execution_id,
        "coverage_snapshot_manifest_sha256": coverage_snapshot_manifest_sha256,
        "prepare_manifest_sha256": prepare_manifest_sha256,
        "security_event_coverage_sha256": expected_security_event_coverage_sha256,
        "corporate_action_coverage_sha256": expected_corporate_action_coverage_sha256,
        "symbols_sha256": hashlib.sha256(("\n".join(symbols) + "\n").encode()).hexdigest(),
        "period": [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()],
        "provider": contract.provider,
        "query_year_type": contract.query_year_type,
        "query_years": list(contract.query_years),
        "symbol_count": len(symbols),
        "successful_query_count": expected_corporate_coverage["coverage"].height,
        "security_event_source": (
            "official_events" if expected_events.height else "official_zero_event_coverage"
        ),
    }
    expected_keys = {*expected, "files"}
    if (
        not isinstance(manifest, dict)
        or set(manifest) != expected_keys
        or any(manifest.get(key) != value for key, value in expected.items())
        or not isinstance(manifest.get("files"), list)
    ):
        raise ValueError("final execution sources cannot be reused across sealed inputs")
    records = _source_file_records(root)
    if manifest["files"] != records:
        raise ValueError("reusable final execution source inventory differs")
    if _source_file_records(root) != records:
        raise ValueError("reusable final execution sources changed during validation")
    return records


def _read_source_frame(path: Path) -> pl.DataFrame:
    if path.is_symlink() or not path.is_file():
        raise ValueError("reusable final execution source file is unsafe")
    try:
        return pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as error:
        raise ValueError("reusable final execution source file is unreadable") from error


def _source_file_records(root: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for directory, directories, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in directories:
            if (current / name).is_symlink():
                raise ValueError("reusable final execution sources contain a symlink")
        for name in filenames:
            path = current / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("reusable final execution sources contain an unsafe file")
            if path == root / "source_manifest.json":
                continue
            records.append(file_record(path, root=root, role="final_execution_source").to_dict())
    return sorted(records, key=lambda record: Path(str(record["path"])))


def validate_security_event_coverage(
    path: Path, *, symbols: list[str] | None = None
) -> dict[str, object]:
    path, coverage_root = _resolve_coverage_manifest(path)
    try:
        manifest_bytes = path.read_bytes()
        payload = json.loads(manifest_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid official security-event coverage") from error
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "ready"
        or payload.get("period") != [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()]
        or payload.get("scope") != "all_final_execution_symbols"
        or not isinstance(payload.get("event_rows"), int)
        or payload["event_rows"] < 0
        or not isinstance(payload.get("symbol_count"), int)
        or payload["symbol_count"] <= 0
        or not isinstance(payload.get("symbols_sha256"), str)
        or len(payload.get("symbols_sha256", "")) != 64
        or not isinstance(payload.get("evidence_index"), dict)
        or not isinstance(payload.get("coverage"), list)
        or not payload["coverage"]
    ):
        raise ValueError("official security-event coverage is not ready")
    evidence_record = payload["evidence_index"]
    if evidence_record.get("role") != "official_security_event_evidence_index":
        raise ValueError("official security-event evidence index role is invalid")
    evidence_index_path = _verify_coverage_file(evidence_record, root=coverage_root)
    evidence_index = pl.read_parquet(evidence_index_path)
    evidence_columns = {"evidence_id", "source", "market", "source_url", "cache_file", "sha256"}
    if (
        not evidence_columns.issubset(evidence_index.columns)
        or evidence_index.select(pl.col("evidence_id").is_duplicated().any()).item()
    ):
        raise ValueError("official security-event evidence index is invalid")
    evidence_paths = [evidence_index_path]
    official_sources = dict(OFFICIAL_MARKET_SOURCES)
    for row in evidence_index.iter_rows(named=True):
        if official_sources.get(str(row["market"])) != row[
            "source"
        ] or not evidence_url_matches_source(str(row["source"]), str(row["source_url"])):
            raise ValueError("official security-event evidence source is invalid")
        cached_input = coverage_root / str(row["cache_file"])
        _assert_no_symlink_path(cached_input, root=coverage_root)
        cached = cached_input.resolve()
        if (
            not cached.is_relative_to(coverage_root)
            or not cached.is_file()
            or sha256_file(cached) != row["sha256"]
        ):
            raise ValueError("official security-event evidence changed")
        evidence_paths.append(cached)
    coverage_paths = []
    for record in payload["coverage"]:
        if not isinstance(record, dict) or record.get("role") != "official_security_event_coverage":
            raise ValueError("official security-event query coverage role is invalid")
        try:
            coverage_paths.append(_verify_coverage_file(record, root=coverage_root))
        except (FileNotFoundError, TypeError, ValueError) as error:
            raise ValueError("official security-event query coverage changed") from error
    coverage = pl.concat([pl.read_parquet(item) for item in coverage_paths])
    required = {
        "symbol",
        "source",
        "market",
        "query_start",
        "query_end",
        "status",
        "event_count",
        "evidence_id",
    }
    if not required.issubset(coverage.columns):
        raise ValueError("official security-event query coverage schema is invalid")
    normalized_coverage = coverage.select(
        pl.col("symbol").cast(pl.String).str.zfill(6).alias("source_symbol"),
        pl.col("source").cast(pl.String),
        pl.col("market").cast(pl.String),
        pl.col("query_start").cast(pl.Date),
        pl.col("query_end").cast(pl.Date),
        pl.col("status").cast(pl.String),
        pl.col("event_count").cast(pl.Int64),
        pl.col("evidence_id").cast(pl.String),
    )
    _validate_coverage_markets(normalized_coverage)
    normalized = (
        sorted({str(symbol).zfill(6) for symbol in symbols})
        if symbols is not None
        else sorted(normalized_coverage.get_column("source_symbol").unique())
    )
    digest = hashlib.sha256(("\n".join(normalized) + "\n").encode()).hexdigest()
    if payload["symbol_count"] != len(normalized) or payload["symbols_sha256"] != digest:
        raise ValueError("official security-event coverage symbol scope changed")
    coverage_keys = ["source_symbol", "market", "source"]
    if normalized_coverage.select(pl.struct(coverage_keys).is_duplicated().any()).item():
        raise ValueError("official security-event query coverage has duplicates")
    if symbols is not None:
        expected = pl.DataFrame({"source_symbol": normalized})
        if (
            normalized_coverage.join(expected, on="source_symbol", how="anti").height
            or expected.join(normalized_coverage, on="source_symbol", how="anti").height
        ):
            raise ValueError("official security-event query coverage is not exact")
    invalid = normalized_coverage.filter(
        (pl.col("status") != "ok")
        | (pl.col("query_start") != FINAL_TEST_START)
        | (pl.col("query_end") != FINAL_TEST_END)
        | (pl.col("event_count") < 0)
    )
    if (
        invalid.height
        or normalized_coverage.join(
            evidence_index.select("evidence_id", "source", "market"),
            on=["evidence_id", "source", "market"],
            how="anti",
        ).height
        or normalized_coverage.get_column("event_count").sum() != payload["event_rows"]
    ):
        raise ValueError("official security-event query coverage is invalid")
    query_record = payload.get("official_query_coverage")
    if not isinstance(query_record, dict) or query_record.get("role") != "official_query_coverage":
        raise ValueError("official security-event query coverage record is invalid")
    try:
        official_query_coverage_path = _verify_coverage_file(
            query_record,
            root=coverage_root,
        )
    except (FileNotFoundError, TypeError, ValueError) as error:
        raise ValueError("official security-event query coverage changed") from error
    official_query_scopes = tuple(
        OfficialQueryScope(
            symbol=symbol,
            market=market_for_symbol(symbol),
            category="security_events",
            query_category="",
            start=FINAL_TEST_START,
            end=FINAL_TEST_END,
        )
        for symbol in normalized
    )
    official_query_index = validate_official_query_coverage_index(
        official_query_coverage_path,
        expected_scopes=official_query_scopes,
    )
    result = dict(payload)
    result["evidence_paths"] = evidence_paths
    result["coverage_paths"] = coverage_paths
    result["evidence_index"] = evidence_index
    result["coverage_root"] = coverage_root
    result["coverage_manifest_path"] = path
    result["coverage_manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    result["official_query_coverage_file"] = official_query_coverage_path
    result["official_query_scopes"] = official_query_scopes
    result["official_query_coverage_index_sha256"] = official_query_index.index_sha256
    event_record = payload.get("events_file")
    normalized_events = pl.DataFrame(
        schema={
            "source_symbol": pl.String,
            "market": pl.String,
            "source": pl.String,
            "evidence_id": pl.String,
        }
    )
    if payload["event_rows"]:
        if not isinstance(event_record, dict):
            raise ValueError("official security-event coverage lacks event file")
        events_path = _verify_coverage_file(event_record, root=coverage_root)
        raw_events = pl.read_parquet(events_path)
        if raw_events.height != payload["event_rows"]:
            raise ValueError("official security events schema or count is invalid")
        events = normalize_security_event_rows(raw_events)
        normalized_events = events.with_columns(
            pl.col("source_symbol")
            .map_elements(market_for_symbol, return_dtype=pl.String)
            .alias("market"),
        )
        for symbol_column in ("source_symbol", "target_symbol"):
            if (
                symbol_column in events.columns
                and normalized_events.filter(
                    pl.col(symbol_column).is_not_null()
                    & ~pl.col(symbol_column)
                    .cast(pl.String)
                    .str.zfill(6)
                    .map_elements(market_for_symbol, return_dtype=pl.String)
                    .is_in(("sh", "sz"))
                ).height
            ):
                raise ValueError("official security events escape supported market scope")
        evidence_mismatch = normalized_events.join(
            evidence_index, on=["evidence_id", "source", "market"], how="anti"
        )
        coverage_mismatch = normalized_events.join(
            normalized_coverage.select(*coverage_keys, "evidence_id"),
            on=[*coverage_keys, "evidence_id"],
            how="anti",
        )
        if (
            evidence_mismatch.height
            or coverage_mismatch.height
            or normalized_events.filter(
                ~pl.col("effective_date").is_between(FINAL_TEST_START, FINAL_TEST_END)
            ).height
        ):
            raise ValueError("official security event evidence join failed")
        result["events_file"] = events_path
        result["events"] = events
    _validate_event_counts(normalized_coverage, normalized_events)
    return result


def _validate_event_counts(coverage: pl.DataFrame, events: pl.DataFrame) -> None:
    """Require exact event counts for every official source/market/security key."""
    keys = ["source_symbol", "market", "source"]
    if "source_symbol" not in coverage.columns and "symbol" in coverage.columns:
        coverage = coverage.rename({"symbol": "source_symbol"})
    expected = coverage.select(*keys, pl.col("event_count").cast(pl.Int64))
    actual = events.group_by(keys).len().rename({"len": "actual_event_count"})
    reconciled = expected.join(actual, on=keys, how="full", coalesce=True).filter(
        pl.col("event_count").is_null()
        | (pl.col("event_count") != pl.col("actual_event_count").fill_null(0))
    )
    if reconciled.height:
        raise ValueError("official security-event event counts do not match coverage")


def _validate_coverage_markets(coverage: pl.DataFrame) -> None:
    expected = coverage.with_columns(
        pl.col("source_symbol")
        .map_elements(market_for_symbol, return_dtype=pl.String)
        .alias("expected_market")
    )
    if expected.filter(pl.col("market") != pl.col("expected_market")).height:
        raise ValueError("official security-event symbol market is invalid")
    allowed = [{"market": market, "source": source} for market, source in OFFICIAL_MARKET_SOURCES]
    if expected.filter(~pl.struct("market", "source").is_in(allowed)).height:
        raise ValueError("official security-event market source is invalid")


def _resolve_coverage_manifest(path: Path) -> tuple[Path, Path]:
    absolute = path if path.is_absolute() else Path.cwd() / path
    absolute = absolute.absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.is_symlink():
            raise ValueError("official security-event coverage uses a symlink")
    try:
        resolved = absolute.resolve(strict=True)
    except FileNotFoundError as error:
        raise ValueError("invalid official security-event coverage") from error
    return resolved, resolved.parent


def _assert_no_symlink_path(path: Path, *, root: Path) -> None:
    try:
        relative = path.absolute().relative_to(root)
    except ValueError as error:
        raise ValueError("official security-event support file escapes coverage root") from error
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("official security-event support file uses a symlink")


def _verify_coverage_file(record: dict[str, object], *, root: Path) -> Path:
    recorded_path = record.get("path")
    if not isinstance(recorded_path, str):
        raise ValueError("official security-event support file path is invalid")
    _assert_no_symlink_path(root / recorded_path, root=root)
    return verify_file_record(record, root=root)


def _download_final_dividends(
    symbols: list[str],
    *,
    years: tuple[int, ...],
    year_type: str,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    try:
        import baostock as bs
    except ImportError as error:  # pragma: no cover - environment failure
        raise RuntimeError("baostock is required for final execution inputs") from error
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
    rows: list[dict[str, object]] = []
    coverage: list[dict[str, object]] = []
    known_fields: list[str] | None = None
    try:
        for symbol in symbols:
            prefix = market_for_symbol(symbol)
            for year in years:
                result = bs.query_dividend_data(
                    code=f"{prefix}.{symbol}",
                    year=str(year),
                    yearType=year_type,
                )
                if result.error_code != "0":
                    raise RuntimeError(
                        f"BaoStock dividend query failed: {symbol} {year} {result.error_msg}"
                    )
                fields = list(result.fields)
                if known_fields is None:
                    known_fields = fields
                elif fields != known_fields:
                    raise ValueError("BaoStock dividend response schema changed")
                count = 0
                while result.next():
                    values = result.get_row_data()
                    if len(values) != len(fields):
                        raise ValueError("BaoStock dividend response row width changed")
                    rows.append(
                        {
                            **dict(zip(fields, values, strict=True)),
                            "query_year": year,
                            "query_year_type": year_type,
                        }
                    )
                    count += 1
                coverage.append(
                    {"symbol": symbol, "year": year, "status": "ok", "row_count": count}
                )
    finally:
        bs.logout()
    schema = {field: pl.String for field in (known_fields or [])}
    schema.update({"query_year": pl.Int64, "query_year_type": pl.String})
    return (
        pl.DataFrame(rows, schema=schema, strict=False),
        pl.DataFrame(
            coverage,
            schema={
                "symbol": pl.String,
                "year": pl.Int64,
                "status": pl.String,
                "row_count": pl.Int64,
            },
        ).sort("symbol", "year"),
    )


def _normalize_final_dividends(
    raw: pl.DataFrame,
    *,
    symbols: set[str],
) -> pl.DataFrame:
    required = {
        "code",
        "query_year",
        "query_year_type",
        "dividOperateDate",
        "dividPayDate",
        "dividStockMarketDate",
        "dividCashPsBeforeTax",
        "dividStocksPs",
        "dividReserveToStockPs",
    }
    missing = sorted(required - set(raw.columns))
    if missing:
        raise ValueError(f"missing BaoStock final dividend fields: {missing}")
    if raw.filter(
        (pl.col("query_year_type") != "operate") | ~pl.col("query_year").is_between(2022, 2025)
    ).height:
        raise ValueError("sealed final-test query metadata is invalid")
    parsed = raw.with_columns(
        pl.col("code").cast(pl.String).str.split(".").list.last().alias("symbol"),
        *(
            pl.col(name).cast(pl.String).replace("", None).str.to_date(strict=False).alias(name)
            for name in ("dividOperateDate", "dividPayDate", "dividStockMarketDate")
        ),
        pl.col("dividCashPsBeforeTax")
        .cast(pl.Float64, strict=False)
        .fill_null(0.0)
        .alias("cash_per_share"),
        (
            pl.col("dividStocksPs").cast(pl.Float64, strict=False).fill_null(0.0)
            + pl.col("dividReserveToStockPs").cast(pl.Float64, strict=False).fill_null(0.0)
        ).alias("share_ratio"),
    )
    if parsed.filter(
        pl.col("dividOperateDate").is_null()
        | (pl.col("dividOperateDate").dt.year() != pl.col("query_year"))
        | ~pl.col("dividOperateDate").is_between(FINAL_TEST_START, FINAL_TEST_END)
    ).height:
        raise ValueError("BaoStock final operate-year response contract failed")
    parsed = parsed.filter(pl.col("symbol").is_in(symbols))
    cash = parsed.filter(pl.col("cash_per_share") > 0)
    if cash.filter(pl.col("dividPayDate").is_null()).height:
        raise ValueError("final cash dividend is missing payment date")
    shares = parsed.filter(pl.col("share_ratio") > 0)
    candidates = pl.concat(
        (
            cash.select(
                "symbol",
                pl.col("dividOperateDate").alias("ex_date"),
                pl.col("dividPayDate").alias("effective_date"),
                "cash_per_share",
                pl.lit(0.0).alias("share_ratio"),
                pl.lit("baostock_dividend_operate_year").alias("source"),
            ),
            shares.select(
                "symbol",
                pl.col("dividOperateDate").alias("ex_date"),
                pl.coalesce("dividStockMarketDate", "dividOperateDate").alias("effective_date"),
                pl.lit(0.0).alias("cash_per_share"),
                "share_ratio",
                pl.lit("baostock_dividend_operate_year").alias("source"),
            ),
        ),
        how="vertical_relaxed",
    )
    if candidates.filter(
        (pl.col("effective_date") < pl.col("ex_date"))
        | ~pl.col("effective_date").is_between(FINAL_TEST_START, FINAL_TEST_END)
    ).height:
        raise ValueError("final corporate action effective date is invalid")
    return normalize_corporate_actions(candidates, maximum_date=FINAL_TEST_END)


def _assert_authorization(authorization: FinalTestAuthorization) -> None:
    if not isinstance(authorization, FinalTestAuthorization):
        raise TypeError("authorization must be a FinalTestAuthorization")
    if authorization.test_period != (FINAL_TEST_START, FINAL_TEST_END):
        raise ValueError("authorization differs from the exact final-test period")
