from datetime import date
import json
import hashlib
import hmac
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.robustness.pipeline import (
    assert_robustness_outputs_sealed,
    compare_robustness_runs,
    finalize_robustness_run,
)
from ashare_multifactor.robustness.protocol import load_robustness_protocol
from ashare_multifactor.robustness.test_protocol import (
    seal_test_protocol,
    verify_test_opening_token,
)


def _identity(*, dirty: bool = False) -> dict[str, object]:
    return {
        "commit": "a" * 40,
        "dirty": dirty,
        "diff_sha256": "b" * 64,
        "sources": [],
        "python": "3.14.6",
        "dependencies": {},
    }


def test_sealed_protocol_binds_code_inputs_rules_metrics_and_one_shot_policy(
    tmp_path: Path,
) -> None:
    protocol = load_robustness_protocol(Path("configs/robustness_protocol.yaml"))
    destination = tmp_path / "sealed_test_protocol.json"
    sealed = seal_test_protocol(
        destination,
        protocol=protocol,
        code_identity=_identity(),
        validation_pointer={
            "run_id": "a4e65bd_stage7_official_evidence_successor",
            "manifest_sha256": "c" * 64,
        },
        market_rules_sha256="d" * 64,
        report_template_sha256="e" * 64,
        opening_ledger_root=tmp_path / "authoritative-opening-ledger",
        gate={"sealed_test_protocol_allowed": True, "status": "ready_to_seal"},
    )

    assert sealed["status"] == "sealed"
    assert sealed["test_period"] == ["2022-01-01", "2025-12-31"]
    assert sealed["supported_markets"] == ["sh", "sz"]
    assert sealed["main_candidate"] == protocol.main_candidate
    assert sealed["metrics"] == list(protocol.final_test_metrics)
    assert sealed["metrics"] != list(protocol.required_metrics)
    assert sealed["one_authoritative_run"] is True
    assert sealed["failed_runs_must_be_retained"] is True
    assert sealed["opening_token_required"] is True
    assert sealed["opening_token_algorithm"] == "HMAC-SHA256-with-external-user-key"
    assert sealed["opening_token_consumption"] == "atomic_one_shot_ledger"
    assert len(sealed["sealed_protocol_sha256"]) == 64
    assert json.loads(destination.read_text()) == sealed

    with pytest.raises(ValueError, match="clean Git identity"):
        seal_test_protocol(
            tmp_path / "dirty.json",
            protocol=protocol,
            code_identity=_identity(dirty=True),
            validation_pointer={"run_id": "x", "manifest_sha256": "y"},
            market_rules_sha256="d" * 64,
            report_template_sha256="e" * 64,
            opening_ledger_root=tmp_path / "authoritative-opening-ledger",
            gate={"sealed_test_protocol_allowed": True},
        )


def test_opening_token_is_rejected_before_any_test_scan(tmp_path: Path) -> None:
    scanned = False
    ledger_root = tmp_path / "authoritative-opening-ledger"
    sealed = {
        "status": "sealed",
        "opening_token_status": "closed",
        "opening_ledger_root": str(ledger_root.resolve()),
    }
    canonical = json.dumps(
        sealed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    seal = hashlib.sha256(canonical).hexdigest()
    sealed["sealed_protocol_sha256"] = seal
    token = tmp_path / "token.json"
    token.write_text(
        json.dumps(
            {
                "status": "approved",
                "approval_id": "user-approved-stage9",
                "sealed_protocol_sha256": seal,
                "signature": "wrong",
            }
        )
    )

    def scanner() -> None:
        nonlocal scanned
        scanned = True

    with pytest.raises(ValueError, match="opening token"):
        verify_test_opening_token(
            token,
            sealed,
            approval_key=b"trusted-user-key",
            before_scan=scanner,
        )
    assert scanned is False


def test_opening_token_requires_external_secret_and_is_consumed_once(tmp_path: Path) -> None:
    seal = "a" * 64
    approval_id = "user-approved-stage9"
    key = b"trusted-user-key"
    signature = hmac.new(
        key,
        f"{seal}|{approval_id}|stage9-one-shot".encode(),
        hashlib.sha256,
    ).hexdigest()
    token = tmp_path / "token.json"
    token.write_text(
        json.dumps(
            {
                "status": "approved",
                "approval_id": approval_id,
                "sealed_protocol_sha256": seal,
                "signature": signature,
            }
        ),
        encoding="utf-8",
    )
    sealed = {
        "status": "sealed",
        "opening_token_status": "closed",
        "opening_ledger_root": str(
            (tmp_path / "authoritative-opening-ledger").resolve()
        ),
        "sealed_protocol_sha256": seal,
    }
    canonical = json.dumps(
        {key: value for key, value in sealed.items() if key != "sealed_protocol_sha256"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    sealed["sealed_protocol_sha256"] = hashlib.sha256(canonical).hexdigest()
    seal = sealed["sealed_protocol_sha256"]
    signature = hmac.new(
        key,
        f"{seal}|{approval_id}|stage9-one-shot".encode(),
        hashlib.sha256,
    ).hexdigest()
    payload = json.loads(token.read_text())
    payload["sealed_protocol_sha256"] = seal
    payload.update({
        "robustness_release": "stage8-release",
        "robustness_manifest_sha256": "b" * 64,
        "robustness_lineage_sha256": "c" * 64,
    })
    identity = f"{seal}|stage8-release|{'b' * 64}|{'c' * 64}"
    payload["signature"] = hmac.new(
        key, f"{identity}|{approval_id}|stage9-one-shot".encode(), hashlib.sha256
    ).hexdigest()
    legacy = dict(payload)
    legacy.pop("robustness_manifest_sha256")
    legacy_token = tmp_path / "legacy-token.json"
    legacy_token.write_text(json.dumps(legacy), encoding="utf-8")
    with pytest.raises(ValueError, match="lacks frozen Stage-8 identity"):
        verify_test_opening_token(legacy_token, sealed, approval_key=key)
    token.write_text(json.dumps(payload), encoding="utf-8")
    verified = verify_test_opening_token(
        token,
        sealed,
        approval_key=key,
    )

    assert verified["approval_id"] == approval_id
    ledger = Path(str(sealed["opening_ledger_root"])) / f"{seal}.json"
    assert json.loads(ledger.read_text())["status"] == "consumed"
    with pytest.raises(ValueError, match="already been consumed"):
        verify_test_opening_token(
            token,
            sealed,
            approval_key=key,
        )

    tampered = dict(sealed, main_candidate="changed-after-sealing")
    with pytest.raises(ValueError, match="sealed protocol hash"):
        verify_test_opening_token(
            token,
            tampered,
            approval_key=key,
        )


def test_robustness_run_finalization_rejects_test_dates_and_compares_hashes(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root in (first, second):
        root.mkdir()
        pl.DataFrame(
            {"date": [date(2021, 12, 31)], "value": [1.0]}
        ).write_parquet(root / "robustness_results.parquet")
        (root / "report.md").write_text("same\n", encoding="utf-8")
        (root / "protocol_gate.json").write_text(
            '{"status":"ready_to_seal"}\n', encoding="utf-8"
        )
        finalize_robustness_run(root, run_id=root.name, identity=_identity())

    comparison = compare_robustness_runs(first, second)
    assert comparison["outputs_identical"] is True
    assert comparison["core_file_count"] == 3

    leaked = tmp_path / "leaked"
    leaked.mkdir()
    pl.DataFrame({"date": [date(2022, 1, 4)]}).write_parquet(leaked / "leak.parquet")
    with pytest.raises(ValueError, match="sealed final test date"):
        assert_robustness_outputs_sealed(leaked)
