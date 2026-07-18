from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import polars as pl
import pytest

from test_build import _config
from test_final_test_gate import (
    APPROVAL_KEY,
    _authorize,
    _commit_all,
    _fixture as gate_fixture,
)

from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.final_test import preparation as preparation_module
from ashare_multifactor.final_test.registry import (
    append_attempt_state,
    register_attempt,
)

from ashare_multifactor.final_test.preparation import (
    _symbols_digest,
    prepare_final_test,
    verify_preparation,
)


FINAL_START = date(2022, 1, 1)
FINAL_END = date(2025, 12, 31)


def _authorization(attempt_id: str = "attempt-001") -> FinalTestAuthorization:
    return FinalTestAuthorization(
        attempt_id=attempt_id,
        approval_id="approved-stage9",
        registered_at="2026-07-17T00:00:00+00:00",
        git_commit="a" * 40,
        git_tree="b" * 40,
        sealed_protocol_sha256="c" * 64,
        robustness_release="stage8-release",
        robustness_manifest_sha256="d" * 64,
        robustness_lineage_sha256="e" * 64,
        test_period=(FINAL_START, FINAL_END),
    )


def _panel(tmp_path: Path, rows: list[tuple[date, str]]) -> tuple[Path, Path]:
    final_root = tmp_path / "processed/final_test"
    panel_root = final_root / "daily_panel"
    partition = panel_root / "year=2022/part-000.parquet"
    partition.parent.mkdir(parents=True)
    pl.DataFrame({"date": [day for day, _ in rows], "symbol": [symbol for _, symbol in rows]}).write_parquet(
        partition
    )
    data_manifest = panel_root / "data_manifest.json"
    data_manifest.write_text('{"schema_version":"1"}\n', encoding="utf-8")
    return panel_root, data_manifest


def _patch_prepare_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    rows: list[tuple[date, str]],
    calls: list[str],
) -> None:
    config = _config(tmp_path)
    panel_root, data_manifest = _panel(tmp_path, rows)
    manifest_sha256 = hashlib.sha256(data_manifest.read_bytes()).hexdigest()
    source = SimpleNamespace(files=(panel_root / "year=2022/part-000.parquet",))
    monkeypatch.setattr(
        "ashare_multifactor.final_test.preparation.authorize_final_test",
        lambda **_kwargs: calls.append("registered") or _authorization(),
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.preparation.append_attempt_state_at",
        lambda _root, **kwargs: calls.append(str(kwargs["state"])),
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.preparation._load_frozen_config",
        lambda _code_root, _data_root: config,
    )

    def build(*_args: object, **_kwargs: object) -> object:
        calls.append("data_scan")
        (tmp_path / "processed/final_test/data-build-claim.json").write_text(
            json.dumps(
                {
                    "attempt_id": "attempt-001",
                    "approval_id": "approved-stage9",
                    "git_commit": "a" * 40,
                    "git_tree": "b" * 40,
                    "sealed_protocol_sha256": "c" * 64,
                    "robustness_release": "stage8-release",
                    "robustness_manifest_sha256": "d" * 64,
                    "robustness_lineage_sha256": "e" * 64,
                    "status": "published",
                    "data_manifest": {
                        "relative_path": "daily_panel/data_manifest.json",
                        "sha256": manifest_sha256,
                        "size_bytes": data_manifest.stat().st_size,
                    },
                }
            ),
            encoding="utf-8",
        )
        return object()

    monkeypatch.setattr(
        "ashare_multifactor.final_test.preparation.build_final_test_daily_panel",
        build,
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.preparation.resolve_final_test_data_panel",
        lambda _root: SimpleNamespace(
            root=panel_root,
            claim_status="published",
            requires_recovery=False,
            data_manifest_sha256=manifest_sha256,
        ),
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.preparation.validate_panel_source",
        lambda _root, _period: source,
    )


def test_prepare_registers_before_scan_and_stops_before_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _patch_prepare_dependencies(
        monkeypatch,
        tmp_path,
        rows=[(date(2022, 1, 3), "000001"), (date(2022, 1, 4), "600000")],
        calls=calls,
    )

    result = prepare_final_test(
        code_root=tmp_path,
        data_root=tmp_path,
        opening_token_path=tmp_path / "token.json",
        approval_key=b"0123456789abcdef",
        attempt_id="attempt-001",
    )

    assert calls == ["registered", "preparing", "data_scan", "awaiting_official_evidence"]
    assert result.state == "awaiting_official_evidence"
    assert result.symbol_count == 2
    assert not (tmp_path / "processed/final_test/attempt_runs").exists()


def test_prepare_publishes_sorted_unique_shenzhen_shanghai_symbol_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _patch_prepare_dependencies(
        monkeypatch,
        tmp_path,
        rows=[
            (date(2022, 1, 3), "600000"),
            (date(2022, 1, 3), "000001"),
            (date(2022, 1, 4), "600000"),
        ],
        calls=calls,
    )

    result = prepare_final_test(
        code_root=tmp_path,
        data_root=tmp_path,
        opening_token_path=tmp_path / "token.json",
        approval_key=b"0123456789abcdef",
        attempt_id="attempt-001",
    )

    assert pl.read_parquet(result.symbol_scope_path).get_column("symbol").to_list() == [
        "000001",
        "600000",
    ]
    assert result.symbols_sha256 == _symbols_digest(["000001", "600000"])
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["period"] == ["2022-01-01", "2025-12-31"]
    assert manifest["supported_markets"] == ["sh", "sz"]
    assert manifest["symbol_scope"]["symbol_count"] == 2
    assert manifest["created_at"] == "2026-07-17T00:00:00+00:00"


@pytest.mark.parametrize("symbol", ["400001", "800001", "920001"])
def test_prepare_rejects_beijing_exchange_symbols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symbol: str
) -> None:
    calls: list[str] = []
    _patch_prepare_dependencies(
        monkeypatch,
        tmp_path,
        rows=[(date(2022, 1, 3), symbol)],
        calls=calls,
    )

    with pytest.raises(ValueError, match="unsupported market"):
        prepare_final_test(
            code_root=tmp_path,
            data_root=tmp_path,
            opening_token_path=tmp_path / "token.json",
            approval_key=b"0123456789abcdef",
            attempt_id="attempt-001",
        )

    assert calls == ["registered", "preparing", "data_scan"]


def test_prepare_rejects_panel_dates_outside_the_sealed_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _patch_prepare_dependencies(
        monkeypatch,
        tmp_path,
        rows=[(date(2026, 1, 2), "000001")],
        calls=calls,
    )

    with pytest.raises(ValueError, match="outside the sealed final-test period"):
        prepare_final_test(
            code_root=tmp_path,
            data_root=tmp_path,
            opening_token_path=tmp_path / "token.json",
            approval_key=b"0123456789abcdef",
            attempt_id="attempt-001",
        )


def test_prepare_rejects_an_existing_preparation_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _patch_prepare_dependencies(
        monkeypatch,
        tmp_path,
        rows=[(date(2022, 1, 3), "000001")],
        calls=calls,
    )
    existing = tmp_path / "processed/final_test/preparations/attempt-001"
    existing.mkdir(parents=True)

    with pytest.raises(FileExistsError, match="preparation already exists"):
        prepare_final_test(
            code_root=tmp_path,
            data_root=tmp_path,
            opening_token_path=tmp_path / "token.json",
            approval_key=b"0123456789abcdef",
            attempt_id="attempt-001",
        )

    assert calls == ["registered", "preparing"]


def test_verify_preparation_rejects_data_manifest_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _patch_prepare_dependencies(
        monkeypatch,
        tmp_path,
        rows=[(date(2022, 1, 3), "000001")],
        calls=calls,
    )
    authorization = _authorization()
    result = prepare_final_test(
        code_root=tmp_path,
        data_root=tmp_path,
        opening_token_path=tmp_path / "token.json",
        approval_key=b"0123456789abcdef",
        attempt_id="attempt-001",
    )
    registry = tmp_path / "processed/final_test/attempts"
    record = register_attempt(
        registry,
        attempt_id=authorization.attempt_id,
        git_commit=authorization.git_commit,
        git_tree=authorization.git_tree,
        token_sha256="f" * 64,
        sealed_protocol_sha256=authorization.sealed_protocol_sha256,
        robustness_release=authorization.robustness_release,
        approval_id=authorization.approval_id,
        robustness_manifest_sha256=authorization.robustness_manifest_sha256,
        robustness_lineage_sha256=authorization.robustness_lineage_sha256,
    )
    authorization = replace(authorization, registered_at=str(record["registered_at"]))
    append_attempt_state(registry, attempt_id="attempt-001", state="preparing")
    append_attempt_state(
        registry,
        attempt_id="attempt-001",
        state="awaiting_official_evidence",
        identities={"prepare_manifest_sha256": result.manifest_sha256},
    )

    assert verify_preparation(
        tmp_path / "processed/final_test",
        attempt_id="attempt-001",
        authorization=authorization,
    ) == result

    (tmp_path / "processed/final_test/daily_panel/data_manifest.json").write_text(
        '{"schema_version":"tampered"}\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="data manifest"):
        verify_preparation(
            tmp_path / "processed/final_test",
            attempt_id="attempt-001",
            authorization=authorization,
        )


def test_prepare_recovers_a_published_scope_without_reauthorizing_or_rescanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _patch_prepare_dependencies(
        monkeypatch,
        tmp_path,
        rows=[(date(2022, 1, 3), "000001")],
        calls=calls,
    )
    result = prepare_final_test(
        code_root=tmp_path,
        data_root=tmp_path,
        opening_token_path=tmp_path / "token.json",
        approval_key=b"0123456789abcdef",
        attempt_id="attempt-001",
    )
    authorization = _authorization()
    registry = tmp_path / "processed/final_test/attempts"
    register_attempt(
        registry,
        attempt_id=authorization.attempt_id,
        git_commit=authorization.git_commit,
        git_tree=authorization.git_tree,
        token_sha256="f" * 64,
        sealed_protocol_sha256=authorization.sealed_protocol_sha256,
        robustness_release=authorization.robustness_release,
        approval_id=authorization.approval_id,
        robustness_manifest_sha256=authorization.robustness_manifest_sha256,
        robustness_lineage_sha256=authorization.robustness_lineage_sha256,
    )
    append_attempt_state(registry, attempt_id="attempt-001", state="preparing")
    calls.clear()
    monkeypatch.setattr(
        "ashare_multifactor.final_test.preparation.recover_registered_authorization",
        lambda **_kwargs: authorization,
    )

    recovered = prepare_final_test(
        code_root=tmp_path,
        data_root=tmp_path,
        opening_token_path=tmp_path / "token.json",
        approval_key=b"0123456789abcdef",
        attempt_id="attempt-001",
    )

    assert recovered == result
    assert calls == ["awaiting_official_evidence"]


def test_verify_preparation_rejects_revalidated_panel_symbol_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _patch_prepare_dependencies(
        monkeypatch,
        tmp_path,
        rows=[(date(2022, 1, 3), "000001")],
        calls=calls,
    )
    result = prepare_final_test(
        code_root=tmp_path,
        data_root=tmp_path,
        opening_token_path=tmp_path / "token.json",
        approval_key=b"0123456789abcdef",
        attempt_id="attempt-001",
    )
    authorization = _authorization()
    registry = tmp_path / "processed/final_test/attempts"
    record = register_attempt(
        registry,
        attempt_id=authorization.attempt_id,
        git_commit=authorization.git_commit,
        git_tree=authorization.git_tree,
        token_sha256="f" * 64,
        sealed_protocol_sha256=authorization.sealed_protocol_sha256,
        robustness_release=authorization.robustness_release,
        approval_id=authorization.approval_id,
        robustness_manifest_sha256=authorization.robustness_manifest_sha256,
        robustness_lineage_sha256=authorization.robustness_lineage_sha256,
    )
    authorization = replace(authorization, registered_at=str(record["registered_at"]))
    append_attempt_state(registry, attempt_id="attempt-001", state="preparing")
    append_attempt_state(
        registry,
        attempt_id="attempt-001",
        state="awaiting_official_evidence",
        identities={"prepare_manifest_sha256": result.manifest_sha256},
    )
    pl.DataFrame({"date": [date(2022, 1, 3)], "symbol": ["600000"]}).write_parquet(
        tmp_path / "processed/final_test/daily_panel/year=2022/part-000.parquet"
    )

    with pytest.raises(ValueError, match="symbol scope differs"):
        verify_preparation(
            tmp_path / "processed/final_test",
            attempt_id="attempt-001",
            authorization=authorization,
        )


def test_verify_preparation_rejects_a_symlink_final_root_before_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _patch_prepare_dependencies(
        monkeypatch,
        tmp_path,
        rows=[(date(2022, 1, 3), "000001")],
        calls=calls,
    )
    result = prepare_final_test(
        code_root=tmp_path,
        data_root=tmp_path,
        opening_token_path=tmp_path / "token.json",
        approval_key=b"0123456789abcdef",
        attempt_id="attempt-001",
    )
    authorization = _authorization()
    registry = tmp_path / "processed/final_test/attempts"
    record = register_attempt(
        registry,
        attempt_id=authorization.attempt_id,
        git_commit=authorization.git_commit,
        git_tree=authorization.git_tree,
        token_sha256="f" * 64,
        sealed_protocol_sha256=authorization.sealed_protocol_sha256,
        robustness_release=authorization.robustness_release,
        approval_id=authorization.approval_id,
        robustness_manifest_sha256=authorization.robustness_manifest_sha256,
        robustness_lineage_sha256=authorization.robustness_lineage_sha256,
    )
    authorization = replace(authorization, registered_at=str(record["registered_at"]))
    append_attempt_state(registry, attempt_id="attempt-001", state="preparing")
    append_attempt_state(
        registry,
        attempt_id="attempt-001",
        state="awaiting_official_evidence",
        identities={"prepare_manifest_sha256": result.manifest_sha256},
    )
    alias = tmp_path / "final-test-alias"
    os.symlink(tmp_path / "processed/final_test", alias)

    with pytest.raises(ValueError, match="uses a symlink"):
        verify_preparation(alias, attempt_id="attempt-001", authorization=authorization)


@pytest.mark.parametrize("crash_after", ["register", "snapshot", "ledger"])
def test_prepare_recovers_half_registered_authorization_before_data_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_after: str,
) -> None:
    paths, data_root, authorization = _gate_attempt(tmp_path)
    registry = paths["registry"]
    snapshot = registry / f"{authorization.attempt_id}.token"
    ledger = (
        paths["opening_ledger"]
        / f"{authorization.sealed_protocol_sha256}.json"
    )
    ledger_payload = json.loads(ledger.read_text(encoding="utf-8"))
    ledger_payload["attempt_id"] = authorization.attempt_id
    ledger.write_text(json.dumps(ledger_payload), encoding="utf-8")
    if crash_after == "register":
        snapshot.unlink()
        ledger.unlink()
    elif crash_after == "snapshot":
        ledger.unlink()
    calls: list[str] = []

    def complete(*_args: object, **_kwargs: object) -> str:
        calls.append("data_access")
        assert snapshot.read_bytes() == paths["token"].read_bytes()
        consumed = json.loads(ledger.read_text(encoding="utf-8"))
        assert consumed["attempt_id"] == authorization.attempt_id
        assert consumed["status"] == "consumed"
        return "prepared"

    monkeypatch.setattr(
        "ashare_multifactor.final_test.preparation._complete_preparation",
        complete,
    )

    result = prepare_final_test(
        code_root=paths["code"],
        data_root=data_root,
        opening_token_path=paths["token"],
        approval_key=APPROVAL_KEY,
        attempt_id=authorization.attempt_id,
    )

    assert result == "prepared"
    assert calls == ["data_access"]


@pytest.mark.parametrize("replacement", ["final_test", "processed"])
def test_preparation_claim_rejects_namespace_replacement_before_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    paths, data_root, authorization = _gate_attempt(tmp_path)
    claim = preparation_module.claim_attempt_preparation_at

    @contextmanager
    def claim_then_replace(*args: object, **kwargs: object):
        with claim(*args, **kwargs):
            processed = data_root / "processed"
            final_root = processed / "final_test"
            target = final_root if replacement == "final_test" else processed
            displaced = tmp_path / f"claimed-{replacement}"
            target.rename(displaced)
            shutil.copytree(displaced, target)
            yield

    monkeypatch.setattr(
        preparation_module, "claim_attempt_preparation_at", claim_then_replace
    )

    with pytest.raises(ValueError, match="claimed final-test root namespace"):
        prepare_final_test(
            code_root=paths["code"],
            data_root=data_root,
            opening_token_path=paths["token"],
            approval_key=APPROVAL_KEY,
            attempt_id=authorization.attempt_id,
        )

    assert not list(tmp_path.rglob("*.state.01-preparing.json"))
    assert not list(tmp_path.rglob("*.outcome.json"))


@pytest.mark.parametrize("drift", ["git", "stage8"])
def test_half_registered_prepare_fails_terminal_before_data_access_on_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    paths, data_root, authorization = _gate_attempt(tmp_path)
    if drift == "git":
        (paths["code"] / "after-register.py").write_text("VALUE = 2\n")
        _commit_all(paths["code"], "drift after register")
    else:
        current = json.loads(
            (data_root / "processed/robustness/CURRENT.json").read_text()
        )
        manifest = (
            data_root
            / "processed/robustness/releases"
            / str(current["run_id"])
            / "manifest.json"
        )
        manifest.write_text('{"changed":true}\n', encoding="utf-8")
    monkeypatch.setattr(
        "ashare_multifactor.final_test.preparation._complete_preparation",
        lambda *_args, **_kwargs: pytest.fail("identity drift reached final data access"),
    )

    with pytest.raises(ValueError):
        prepare_final_test(
            code_root=paths["code"],
            data_root=data_root,
            opening_token_path=paths["token"],
            approval_key=APPROVAL_KEY,
            attempt_id=authorization.attempt_id,
        )

    outcome = json.loads(
        (
            paths["registry"]
            / f"{authorization.attempt_id}.outcome.json"
        ).read_text(encoding="utf-8")
    )
    assert outcome["status"] == "failed"


def test_concurrent_prepare_serializes_one_recoverable_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, data_root, authorization = _gate_attempt(tmp_path)
    ledger = (
        paths["opening_ledger"]
        / f"{authorization.sealed_protocol_sha256}.json"
    )
    ledger_payload = json.loads(ledger.read_text(encoding="utf-8"))
    ledger_payload["attempt_id"] = authorization.attempt_id
    ledger.write_text(json.dumps(ledger_payload), encoding="utf-8")
    registry = paths["registry"]
    completions = 0

    def complete(*_args: object, **_kwargs: object) -> str:
        nonlocal completions
        completions += 1
        append_attempt_state(
            registry,
            attempt_id=authorization.attempt_id,
            state="awaiting_official_evidence",
            identities={"prepare_manifest_sha256": "a" * 64},
        )
        return "prepared"

    monkeypatch.setattr(
        "ashare_multifactor.final_test.preparation._complete_preparation",
        complete,
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.preparation.verify_preparation",
        lambda *_args, **_kwargs: "prepared",
    )

    def run() -> str:
        return prepare_final_test(
            code_root=paths["code"],
            data_root=data_root,
            opening_token_path=paths["token"],
            approval_key=APPROVAL_KEY,
            attempt_id=authorization.attempt_id,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result(timeout=10) for future in (executor.submit(run), executor.submit(run))]

    assert results == ["prepared", "prepared"]
    assert completions == 1


def test_failed_data_claim_closes_same_attempt_and_preserves_audit(
    tmp_path: Path,
) -> None:
    paths, data_root, authorization = _gate_attempt(tmp_path)
    final_root = data_root / "processed/final_test"
    claim = {
        "attempt_id": authorization.attempt_id,
        "approval_id": authorization.approval_id,
        "git_commit": authorization.git_commit,
        "git_tree": authorization.git_tree,
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "robustness_release": authorization.robustness_release,
        "robustness_manifest_sha256": authorization.robustness_manifest_sha256,
        "robustness_lineage_sha256": authorization.robustness_lineage_sha256,
        "status": "failed",
        "error_type": "ValueError",
        "error": "synthetic data failure",
    }
    claim_path = final_root / "data-build-claim.json"
    claim_path.write_text(json.dumps(claim), encoding="utf-8")

    with pytest.raises(ValueError, match="failed.*cannot be recovered"):
        prepare_final_test(
            code_root=paths["code"],
            data_root=data_root,
            opening_token_path=paths["token"],
            approval_key=APPROVAL_KEY,
            attempt_id=authorization.attempt_id,
        )

    assert json.loads(claim_path.read_text(encoding="utf-8")) == claim
    outcome = json.loads(
        (
            paths["registry"]
            / f"{authorization.attempt_id}.outcome.json"
        ).read_text(encoding="utf-8")
    )
    assert outcome["status"] == "failed"


def _gate_attempt(
    tmp_path: Path,
) -> tuple[dict[str, Path], Path, FinalTestAuthorization]:
    paths = gate_fixture(tmp_path)
    data_root = tmp_path / "data"
    processed = data_root / "processed"
    processed.mkdir(parents=True)
    robustness = processed / "robustness"
    validation = processed / "validation_evaluation"
    registry = processed / "final_test/attempts"
    shutil.move(paths["robustness"], robustness)
    shutil.move(paths["validation"], validation)
    paths.update(
        {"robustness": robustness, "validation": validation, "registry": registry}
    )
    return paths, data_root, _authorize(paths)
