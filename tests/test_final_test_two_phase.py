from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import polars as pl
import pytest

from test_corporate_action_coverage import _write_coverage as write_corporate_coverage
from test_final_test_gate import APPROVAL_KEY, _fixture as gate_fixture
from test_final_test_pipeline import _patch_steps
from test_final_test_resume import _write_security_coverage

from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.cli import final_test as cli_module
from ashare_multifactor.final_test import pipeline as pipeline_module
from ashare_multifactor.final_test import preparation as preparation_module
from ashare_multifactor.final_test.registry import resolve_attempt_state


@dataclass(frozen=True)
class TwoPhaseFixture:
    code_root: Path
    data_root: Path
    opening_token_path: Path
    approval_key: bytes
    opening_ledger_root: Path
    security_event_coverage_path: Path
    corporate_action_coverage_root: Path


def test_cli_requires_explicit_prepare_or_resume() -> None:
    with pytest.raises(SystemExit) as error:
        cli_module.main([])

    assert error.value.code == 2


def test_cli_prepare_rejects_resume_coverage_arguments(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as error:
        cli_module.main(
            [
                "prepare",
                "--data-root",
                str(tmp_path),
                "--opening-token",
                str(tmp_path / "token.json"),
                "--approval-key-file",
                str(tmp_path / "key"),
                "--attempt-id",
                "attempt-001",
                "--security-event-coverage",
                str(tmp_path / "security.json"),
            ]
        )

    assert error.value.code == 2


def test_cli_resume_rejects_opening_token_argument(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as error:
        cli_module.main(
            [
                "resume",
                "--data-root",
                str(tmp_path),
                "--approval-key-file",
                str(tmp_path / "key"),
                "--attempt-id",
                "attempt-001",
                "--security-event-coverage",
                str(tmp_path / "security.json"),
                "--corporate-action-coverage",
                str(tmp_path / "actions"),
                "--run-id",
                "final-release",
                "--opening-token",
                str(tmp_path / "token.json"),
            ]
        )

    assert error.value.code == 2


def test_cli_prepare_routes_only_preparation_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = tmp_path / "approval.key"
    key.write_bytes(b"synthetic-key")
    calls: list[dict[str, object]] = []
    output = tmp_path / "preparations/attempt-001"
    monkeypatch.setattr(
        cli_module,
        "prepare_final_test",
        lambda **kwargs: calls.append(kwargs) or SimpleNamespace(root=output),
    )

    cli_module.main(
        [
            "prepare",
            "--root",
            str(tmp_path / "code"),
            "--data-root",
            str(tmp_path / "data"),
            "--opening-token",
            str(tmp_path / "token.json"),
            "--approval-key-file",
            str(key),
            "--attempt-id",
            "attempt-001",
        ]
    )

    assert calls == [
        {
            "code_root": tmp_path / "code",
            "data_root": tmp_path / "data",
            "opening_token_path": tmp_path / "token.json",
            "approval_key": b"synthetic-key",
            "attempt_id": "attempt-001",
        }
    ]
    assert capsys.readouterr().out.strip() == str(output)


def test_cli_resume_routes_only_release_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = tmp_path / "approval.key"
    key.write_bytes(b"synthetic-key")
    calls: list[dict[str, object]] = []
    release_root = tmp_path / "releases/final-release"
    monkeypatch.setattr(
        cli_module,
        "resume_final_test_release",
        lambda **kwargs: calls.append(kwargs)
        or SimpleNamespace(release=SimpleNamespace(root=release_root), attempt_root=None),
    )

    cli_module.main(
        [
            "resume",
            "--root",
            str(tmp_path / "code"),
            "--data-root",
            str(tmp_path / "data"),
            "--approval-key-file",
            str(key),
            "--attempt-id",
            "attempt-001",
            "--security-event-coverage",
            str(tmp_path / "security.json"),
            "--corporate-action-coverage",
            str(tmp_path / "actions"),
            "--run-id",
            "final-release",
        ]
    )

    assert calls == [
        {
            "code_root": tmp_path / "code",
            "data_root": tmp_path / "data",
            "approval_key": b"synthetic-key",
            "attempt_id": "attempt-001",
            "security_event_coverage_path": tmp_path / "security.json",
            "corporate_action_coverage_root": tmp_path / "actions",
            "run_id": "final-release",
        }
    ]
    assert capsys.readouterr().out.strip() == str(release_root)


def test_synthetic_two_phase_flow_uses_one_approval_and_one_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = build_two_phase_fixture(tmp_path, monkeypatch)
    prepared = preparation_module.prepare_final_test(
        code_root=fixture.code_root,
        data_root=fixture.data_root,
        opening_token_path=fixture.opening_token_path,
        approval_key=fixture.approval_key,
        attempt_id="attempt-001",
    )

    assert prepared.state == "awaiting_official_evidence"
    assert _token_consumption_count(fixture.opening_ledger_root) == 1
    assert _attempt_ids(fixture.data_root) == {"attempt-001"}
    attempt_root = fixture.data_root / "processed/final_test/attempt_runs/attempt-001"
    assert not attempt_root.exists()
    assert not (fixture.data_root / "processed/final_test/CURRENT.json").exists()

    _write_exact_official_coverages(fixture, prepared.symbol_scope_path)
    _patch_steps(monkeypatch, publishable=True)
    result = pipeline_module.resume_final_test_release(
        code_root=fixture.code_root,
        data_root=fixture.data_root,
        approval_key=fixture.approval_key,
        attempt_id="attempt-001",
        security_event_coverage_path=fixture.security_event_coverage_path,
        corporate_action_coverage_root=fixture.corporate_action_coverage_root,
        run_id="final-release",
    )

    assert result.release is not None
    assert result.release.run_id == "final-release"
    assert _attempt_ids(fixture.data_root) == {"attempt-001"}
    assert _token_consumption_count(fixture.opening_ledger_root) == 1
    with pytest.raises(ValueError, match="already succeeded|state transition"):
        pipeline_module.resume_final_test_release(
            code_root=fixture.code_root,
            data_root=fixture.data_root,
            approval_key=fixture.approval_key,
            attempt_id="attempt-001",
            security_event_coverage_path=fixture.security_event_coverage_path,
            corporate_action_coverage_root=fixture.corporate_action_coverage_root,
            run_id="final-release",
        )


def test_synthetic_prepare_rejects_beijing_exchange_symbol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = build_two_phase_fixture(
        tmp_path, monkeypatch, symbols=("000001", "920001")
    )

    with pytest.raises(ValueError, match="unsupported market"):
        preparation_module.prepare_final_test(
            code_root=fixture.code_root,
            data_root=fixture.data_root,
            opening_token_path=fixture.opening_token_path,
            approval_key=fixture.approval_key,
            attempt_id="attempt-001",
        )

    registry = fixture.data_root / "processed/final_test/attempts"
    assert resolve_attempt_state(registry, "attempt-001")["state"] == "failed"
    assert _token_consumption_count(fixture.opening_ledger_root) == 1


def build_two_phase_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    symbols: tuple[str, ...] = ("000001", "600000"),
) -> TwoPhaseFixture:
    paths = gate_fixture(tmp_path)
    data_root = tmp_path / "data"
    processed = data_root / "processed"
    processed.mkdir(parents=True)
    shutil.move(paths["robustness"], processed / "robustness")
    shutil.move(paths["validation"], processed / "validation_evaluation")
    final_root = processed / "final_test"
    panel_root = final_root / "daily_panel"
    partition = panel_root / "year=2022/part-000.parquet"
    data_manifest = panel_root / "data_manifest.json"

    def build_panel(_config: object, authorization: object, *_args: object, **_kwargs: object) -> None:
        partition.parent.mkdir(parents=True)
        pl.DataFrame(
            {
                "date": [date(2022, 1, 4)] * len(symbols),
                "symbol": list(symbols),
            }
        ).write_parquet(partition)
        data_manifest.write_text('{"schema_version":"synthetic"}\n', encoding="utf-8")
        claim = {
            field: getattr(authorization, field)
            for field in (
                "attempt_id",
                "approval_id",
                "git_commit",
                "git_tree",
                "sealed_protocol_sha256",
                "robustness_release",
                "robustness_manifest_sha256",
                "robustness_lineage_sha256",
            )
        }
        claim.update(
            {
                "status": "published",
                "data_manifest": {"sha256": sha256_file(data_manifest)},
            }
        )
        (final_root / "data-build-claim.json").write_text(
            json.dumps(claim), encoding="utf-8"
        )

    def resolve_panel(_root: Path) -> SimpleNamespace:
        return SimpleNamespace(
            root=panel_root,
            claim_status="published",
            requires_recovery=False,
            data_manifest_sha256=sha256_file(data_manifest),
        )

    monkeypatch.setattr(preparation_module, "build_final_test_daily_panel", build_panel)
    monkeypatch.setattr(preparation_module, "resolve_final_test_data_panel", resolve_panel)
    monkeypatch.setattr(
        preparation_module,
        "resolve_final_test_data_panel_at",
        lambda _root, _fd: resolve_panel(_root),
    )
    monkeypatch.setattr(
        preparation_module,
        "validate_panel_source",
        lambda _root, _period: SimpleNamespace(files=(partition,)),
    )
    monkeypatch.setattr(
        preparation_module,
        "_symbols_from_verified_panel_at",
        lambda _fd: sorted(symbols),
    )
    return TwoPhaseFixture(
        code_root=paths["code"],
        data_root=data_root,
        opening_token_path=paths["token"],
        approval_key=APPROVAL_KEY,
        opening_ledger_root=paths["opening_ledger"],
        security_event_coverage_path=tmp_path / "security-coverage/coverage.json",
        corporate_action_coverage_root=tmp_path / "corporate-coverage",
    )


def _write_exact_official_coverages(
    fixture: TwoPhaseFixture, symbol_scope_path: Path
) -> None:
    symbols = pl.read_parquet(symbol_scope_path).get_column("symbol").to_list()
    _write_security_coverage(
        fixture.security_event_coverage_path.parent, symbols=symbols
    )
    write_corporate_coverage(
        fixture.corporate_action_coverage_root, symbols=tuple(symbols)
    )


def _token_consumption_count(opening_ledger_root: Path) -> int:
    return len(list(opening_ledger_root.glob("*.json")))


def _attempt_ids(data_root: Path) -> set[str]:
    registry = data_root / "processed/final_test/attempts"
    attempt_ids: set[str] = set()
    for path in registry.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        attempt_id = payload.get("attempt_id")
        if isinstance(attempt_id, str):
            attempt_ids.add(attempt_id)
    return attempt_ids
