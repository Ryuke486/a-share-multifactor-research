from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import polars as pl
import pytest

from test_corporate_action_coverage import _write_coverage as write_corporate_coverage
from test_final_test_gate import (
    APPROVAL_KEY,
    _authorize,
    _commit_all,
    _fixture as gate_fixture,
)

from ashare_multifactor.audit.records import file_record, sha256_file
from ashare_multifactor.final_test import execution_sources as execution_sources_module
from ashare_multifactor.final_test import gate as gate_module
from ashare_multifactor.final_test import resume as resume_module
from ashare_multifactor.final_test.preparation import _publish_preparation
from ashare_multifactor.final_test.registry import (
    append_attempt_state,
)
from ashare_multifactor.final_test.resume import (
    load_registered_authorization,
    preflight_resume,
)


@dataclass(frozen=True)
class PreparedAttempt:
    code_root: Path
    data_root: Path
    attempt_id: str
    approval_key: bytes
    consumption_ledger: Path
    preparation_manifest: Path
    symbol_scope: Path
    security_coverage: Path
    corporate_coverage: Path


@pytest.fixture
def prepared_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PreparedAttempt:
    paths = gate_fixture(tmp_path)
    data_root = tmp_path / "data"
    processed = data_root / "processed"
    processed.mkdir(parents=True)
    robustness = processed / "robustness"
    validation = processed / "validation_evaluation"
    final_root = processed / "final_test"
    registry = final_root / "attempts"
    shutil.move(paths["robustness"], robustness)
    shutil.move(paths["validation"], validation)
    paths.update({"robustness": robustness, "validation": validation, "registry": registry})
    authorization = _authorize(paths)

    panel_root = final_root / "daily_panel"
    partition = panel_root / "year=2022/part-000.parquet"
    partition.parent.mkdir(parents=True)
    symbols = ["000001", "600000"]
    pl.DataFrame(
        {
            "date": [date(2022, 1, 3), date(2022, 1, 3)],
            "symbol": symbols,
        }
    ).write_parquet(partition)
    data_manifest = panel_root / "data_manifest.json"
    data_manifest.write_text('{"schema_version":"1"}\n', encoding="utf-8")
    resolution = SimpleNamespace(
        root=panel_root,
        claim_status="published",
        requires_recovery=False,
        data_manifest_sha256=sha256_file(data_manifest),
    )
    preparation = _publish_preparation(
        final_root,
        authorization=authorization,
        resolution=resolution,
        symbols=symbols,
    )
    append_attempt_state(registry, attempt_id=authorization.attempt_id, state="preparing")
    append_attempt_state(
        registry,
        attempt_id=authorization.attempt_id,
        state="awaiting_official_evidence",
        identities={"prepare_manifest_sha256": preparation.manifest_sha256},
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.preparation.resolve_final_test_data_panel",
        lambda _root: resolution,
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.preparation.validate_panel_source",
        lambda _root, _period: SimpleNamespace(files=(partition,)),
    )

    security_root = tmp_path / "security-coverage"
    security_root.mkdir()
    security_coverage = _write_security_coverage(security_root, symbols=symbols)
    corporate_coverage = write_corporate_coverage(
        tmp_path / "corporate-coverage", symbols=tuple(symbols)
    )
    consumption_ledger = paths["opening_ledger"] / f"{authorization.sealed_protocol_sha256}.json"
    return PreparedAttempt(
        code_root=paths["code"],
        data_root=data_root,
        attempt_id=authorization.attempt_id,
        approval_key=APPROVAL_KEY,
        consumption_ledger=consumption_ledger,
        preparation_manifest=preparation.manifest_path,
        symbol_scope=preparation.symbol_scope_path,
        security_coverage=security_coverage,
        corporate_coverage=corporate_coverage,
    )


def test_resume_reconstructs_same_authorization_without_consuming_token_again(
    prepared_attempt: PreparedAttempt, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = prepared_attempt.consumption_ledger.read_bytes()
    monkeypatch.setattr(
        "ashare_multifactor.final_test.gate.authorize_final_test",
        lambda **_kwargs: pytest.fail("resume must not authorize again"),
    )
    monkeypatch.setattr(
        "ashare_multifactor.robustness.test_protocol.verify_test_opening_token",
        lambda *_args, **_kwargs: pytest.fail("resume must not consume token again"),
    )

    authorization = load_registered_authorization(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        attempt_id=prepared_attempt.attempt_id,
        approval_key=prepared_attempt.approval_key,
    )

    assert authorization.attempt_id == prepared_attempt.attempt_id
    assert prepared_attempt.consumption_ledger.read_bytes() == before


def test_resume_uses_git_with_optional_locks_disabled(
    prepared_attempt: PreparedAttempt, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_run = subprocess.run
    git_environments: list[dict[str, str] | None] = []

    def capture_run(*args: object, **kwargs: object):
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, tuple) and command and command[0] == "git":
            environment = kwargs.get("env")
            git_environments.append(environment if isinstance(environment, dict) else None)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(gate_module.subprocess, "run", capture_run)

    load_registered_authorization(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        attempt_id=prepared_attempt.attempt_id,
        approval_key=prepared_attempt.approval_key,
    )

    assert git_environments
    assert all(
        environment is not None and environment.get("GIT_OPTIONAL_LOCKS") == "0"
        for environment in git_environments
    )


def test_resume_rejects_missing_attempt_lock_without_creating_files(
    prepared_attempt: PreparedAttempt,
) -> None:
    registry = prepared_attempt.data_root / "processed/final_test/attempts"
    lock = registry / f"{prepared_attempt.attempt_id}.lock"
    lock.unlink()
    before = _registry_snapshot(prepared_attempt)

    with pytest.raises(ValueError, match="lock"):
        _preflight(prepared_attempt)

    assert _registry_snapshot(prepared_attempt) == before
    assert not lock.exists()


@pytest.mark.parametrize(
    "changed",
    ["registration", "token", "ledger", "stage8_manifest", "lineage", "seal", "git"],
)
def test_resume_rejects_registered_authorization_identity_drift(
    prepared_attempt: PreparedAttempt, changed: str
) -> None:
    registry = prepared_attempt.data_root / "processed/final_test/attempts"
    if changed == "registration":
        path = registry / f"{prepared_attempt.attempt_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["token_sha256"] = "0" * 64
        path.write_text(json.dumps(payload), encoding="utf-8")
    elif changed == "token":
        (registry / f"{prepared_attempt.attempt_id}.token").write_bytes(b"changed")
    elif changed == "ledger":
        prepared_attempt.consumption_ledger.write_text(
            '{"status":"consumed","approval_id":"changed"}\n', encoding="utf-8"
        )
    elif changed == "git":
        (prepared_attempt.code_root / "after-approval.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )
        _commit_all(prepared_attempt.code_root, "code after approval")
    else:
        stage8 = prepared_attempt.data_root / "processed/robustness"
        current = json.loads((stage8 / "CURRENT.json").read_text(encoding="utf-8"))
        release = stage8 / "releases" / str(current["run_id"])
        relative = {
            "stage8_manifest": "manifest.json",
            "lineage": "lineage.json",
            "seal": "artifacts/sealed_test_protocol.json",
        }[changed]
        (release / relative).write_text('{"changed":true}\n', encoding="utf-8")

    with pytest.raises(ValueError):
        load_registered_authorization(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            attempt_id=prepared_attempt.attempt_id,
            approval_key=prepared_attempt.approval_key,
        )


def test_resume_rejects_current_stage8_run_id_drift(
    prepared_attempt: PreparedAttempt,
) -> None:
    stage8 = prepared_attempt.data_root / "processed/robustness"
    current_path = stage8 / "CURRENT.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    original = stage8 / "releases" / str(current["run_id"])
    replacement_id = "replacement-stage8-run"
    shutil.copytree(original, stage8 / "releases" / replacement_id)
    current["run_id"] = replacement_id
    current_path.write_text(json.dumps(current), encoding="utf-8")

    with pytest.raises(ValueError, match="Stage-8 identity"):
        load_registered_authorization(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            attempt_id=prepared_attempt.attempt_id,
            approval_key=prepared_attempt.approval_key,
        )


def test_preflight_verifies_preparation_before_reading_coverages(
    prepared_attempt: PreparedAttempt,
) -> None:
    prepared_attempt.preparation_manifest.write_text(
        prepared_attempt.preparation_manifest.read_text(encoding="utf-8") + " ",
        encoding="utf-8",
    )
    prepared_attempt.security_coverage.unlink()

    with pytest.raises(ValueError, match="preparation manifest"):
        _preflight(prepared_attempt)


def test_preflight_rejects_symbol_scope_drift(
    prepared_attempt: PreparedAttempt,
) -> None:
    pl.DataFrame({"symbol": ["000001"]}).write_parquet(prepared_attempt.symbol_scope)

    with pytest.raises(ValueError, match="symbol scope"):
        _preflight(prepared_attempt)


def test_preflight_rejects_symbol_scope_reordered_after_preparation_verification(
    prepared_attempt: PreparedAttempt, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_verify = resume_module.verify_preparation

    def reorder_after_verify(*args: object, **kwargs: object):
        preparation = original_verify(*args, **kwargs)
        pl.DataFrame({"symbol": ["600000", "000001"]}).write_parquet(preparation.symbol_scope_path)
        return preparation

    monkeypatch.setattr(resume_module, "verify_preparation", reorder_after_verify)

    with pytest.raises(ValueError, match="symbol scope"):
        _preflight(prepared_attempt)


def test_preflight_requires_awaiting_official_evidence_state(
    prepared_attempt: PreparedAttempt,
) -> None:
    state_path = next(
        (prepared_attempt.data_root / "processed/final_test/attempts").glob(
            "*.state.02-awaiting_official_evidence.json"
        )
    )
    state_path.unlink()

    with pytest.raises(ValueError, match="awaiting official evidence"):
        _preflight(prepared_attempt)


@pytest.mark.parametrize(
    ("coverage", "symbols"),
    [
        ("security", ["000001"]),
        ("security", ["000001", "300001", "600000"]),
        ("corporate", ["000001"]),
        ("corporate", ["000001", "300001", "600000"]),
    ],
)
def test_preflight_rejects_missing_or_extra_coverage_symbols_without_state_change(
    prepared_attempt: PreparedAttempt, coverage: str, symbols: list[str]
) -> None:
    if coverage == "security":
        _write_security_coverage(prepared_attempt.security_coverage.parent, symbols=symbols)
    else:
        shutil.rmtree(prepared_attempt.corporate_coverage)
        write_corporate_coverage(prepared_attempt.corporate_coverage, symbols=tuple(symbols))

    before = _registry_snapshot(prepared_attempt)
    with pytest.raises(ValueError, match="symbol scope|not exact"):
        _preflight(prepared_attempt)
    assert _registry_snapshot(prepared_attempt) == before
    assert _attempt_state(prepared_attempt) == "awaiting_official_evidence"


def test_preflight_rejects_zero_event_without_successful_official_evidence(
    prepared_attempt: PreparedAttempt,
) -> None:
    coverage_file = prepared_attempt.security_coverage.parent / "query_coverage.parquet"
    pl.read_parquet(coverage_file).with_columns(pl.lit("failed").alias("status")).write_parquet(
        coverage_file
    )
    _refresh_security_record(prepared_attempt.security_coverage)
    before = _registry_snapshot(prepared_attempt)

    with pytest.raises(ValueError, match="query coverage is invalid"):
        _preflight(prepared_attempt)

    assert _registry_snapshot(prepared_attempt) == before
    assert _attempt_state(prepared_attempt) == "awaiting_official_evidence"


@pytest.mark.parametrize("coverage", ["security", "corporate"])
def test_preflight_rejects_coverage_evidence_hash_drift_without_state_change(
    prepared_attempt: PreparedAttempt, coverage: str
) -> None:
    if coverage == "security":
        changed = prepared_attempt.security_coverage.parent / "szse.pdf"
    else:
        changed = prepared_attempt.corporate_coverage / "szse.pdf"
    changed.write_bytes(b"changed")
    before = _registry_snapshot(prepared_attempt)

    with pytest.raises(ValueError, match="evidence.*changed|evidence hash changed"):
        _preflight(prepared_attempt)

    assert _registry_snapshot(prepared_attempt) == before
    assert _attempt_state(prepared_attempt) == "awaiting_official_evidence"


@pytest.mark.parametrize("coverage", ["security", "corporate"])
def test_preflight_rejects_manifest_change_during_coverage_validation(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
    coverage: str,
) -> None:
    attribute = (
        "validate_security_event_coverage"
        if coverage == "security"
        else "validate_corporate_action_coverage"
    )
    original_validate = getattr(execution_sources_module, attribute)

    def mutate_after_validate(*args: object, **kwargs: object):
        verified = original_validate(*args, **kwargs)
        manifest = verified["coverage_manifest_path"]
        assert isinstance(manifest, Path)
        manifest.write_text('{"changed":true}\n', encoding="utf-8")
        return verified

    monkeypatch.setattr(execution_sources_module, attribute, mutate_after_validate)

    with pytest.raises(ValueError, match="changed during validation"):
        _preflight(prepared_attempt)


def test_preflight_returns_verified_coverage_manifest_hashes(
    prepared_attempt: PreparedAttempt,
) -> None:
    before = _registry_snapshot(prepared_attempt)

    result = _preflight(prepared_attempt)

    assert result.authorization.attempt_id == prepared_attempt.attempt_id
    assert result.preparation.symbol_count == 2
    assert result.security_event_coverage_sha256 == sha256_file(prepared_attempt.security_coverage)
    assert result.corporate_action_coverage_sha256 == sha256_file(
        prepared_attempt.corporate_coverage / "coverage.json"
    )
    assert _registry_snapshot(prepared_attempt) == before
    assert _attempt_state(prepared_attempt) == "awaiting_official_evidence"


def _preflight(attempt: PreparedAttempt):
    return preflight_resume(
        code_root=attempt.code_root,
        data_root=attempt.data_root,
        attempt_id=attempt.attempt_id,
        approval_key=attempt.approval_key,
        security_event_coverage_path=attempt.security_coverage,
        corporate_action_coverage_root=attempt.corporate_coverage,
    )


def _registry_snapshot(attempt: PreparedAttempt) -> dict[str, bytes]:
    registry = attempt.data_root / "processed/final_test/attempts"
    return {path.name: path.read_bytes() for path in sorted(registry.glob("*"))}


def _attempt_state(attempt: PreparedAttempt) -> str:
    registry = attempt.data_root / "processed/final_test/attempts"
    return str(resume_module.resolve_attempt_state_readonly(registry, attempt.attempt_id)["state"])


def _write_security_coverage(root: Path, *, symbols: list[str]) -> Path:
    root.mkdir(exist_ok=True)
    evidence_rows = []
    for market, source in (("sz", "szse"), ("sh", "sse")):
        evidence = root / f"{source}.pdf"
        evidence.write_bytes(source.encode())
        evidence_rows.append(
            {
                "evidence_id": f"ev-{market}",
                "source": source,
                "market": market,
                "source_url": (
                    "https://disc.static.szse.cn/download/disc/security.pdf"
                    if market == "sz"
                    else "https://www.sse.com.cn/disclosure/listedinfo/announcement/security.pdf"
                ),
                "cache_file": evidence.name,
                "sha256": sha256_file(evidence),
            }
        )
    evidence_index = root / "evidence_index.parquet"
    pl.DataFrame(evidence_rows).write_parquet(evidence_index)
    coverage_rows = []
    for symbol in symbols:
        market = "sz" if symbol.startswith(("0", "3")) else "sh"
        source = "szse" if market == "sz" else "sse"
        coverage_rows.append(
            {
                "symbol": symbol,
                "source": source,
                "market": market,
                "query_start": date(2022, 1, 1),
                "query_end": date(2025, 12, 31),
                "status": "ok",
                "event_count": 0,
                "evidence_id": f"ev-{market}",
            }
        )
    query_coverage = root / "query_coverage.parquet"
    pl.DataFrame(coverage_rows).write_parquet(query_coverage)
    normalized = sorted(symbols)
    path = root / "coverage.json"
    path.write_text(
        json.dumps(
            {
                "status": "ready",
                "period": ["2022-01-01", "2025-12-31"],
                "scope": "all_final_execution_symbols",
                "symbol_count": len(normalized),
                "symbols_sha256": hashlib.sha256(
                    ("\n".join(normalized) + "\n").encode()
                ).hexdigest(),
                "event_rows": 0,
                "evidence_index": file_record(
                    evidence_index,
                    root=root,
                    role="official_security_event_evidence_index",
                ).to_dict(),
                "coverage": [
                    file_record(
                        query_coverage,
                        root=root,
                        role="official_security_event_coverage",
                    ).to_dict()
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _refresh_security_record(path: Path) -> None:
    root = path.parent
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["coverage"] = [
        file_record(
            root / "query_coverage.parquet",
            root=root,
            role="official_security_event_coverage",
        ).to_dict()
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")
