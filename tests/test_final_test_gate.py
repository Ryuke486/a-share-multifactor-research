from __future__ import annotations

from collections.abc import Callable
from datetime import date
import hashlib
import hmac
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from ashare_multifactor.audit.identity import code_identity
from ashare_multifactor.audit.publication import publish_release, resolve_current
from ashare_multifactor.audit.records import file_record, sha256_file
from ashare_multifactor.final_test.data_inventory import write_json
from ashare_multifactor.final_test.gate import authorize_final_test
from ashare_multifactor.final_test import gate as gate_module
from ashare_multifactor.robustness.protocol import load_robustness_protocol
from ashare_multifactor.robustness.test_protocol import seal_test_protocol


APPROVAL_KEY = b"stage9-explicit-user-key"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit_all(root: Path, message: str) -> None:
    _git(root, "add", ".")
    _git(root, "commit", "-m", message)


def _staging(root: Path, *, artifact: str) -> tuple[Path, Path]:
    datasets = root / "datasets"
    artifacts = root / "artifacts"
    datasets.mkdir(parents=True)
    artifacts.mkdir()
    (datasets / "placeholder.bin").write_bytes(b"sealed upstream")
    (artifacts / artifact).write_text("sealed\n", encoding="utf-8")
    return datasets, artifacts


def _write_token(path: Path, seal: str, code_root: Path, robustness: object) -> None:
    approval_id = "user-approved-stage9"
    commit = _git(code_root, "rev-parse", "HEAD")
    tree = _git(code_root, "rev-parse", "HEAD^{tree}")
    run_id = str(getattr(robustness, "run_id"))
    manifest_sha = str(getattr(robustness, "manifest_sha256"))
    lineage_sha = sha256_file(Path(getattr(robustness, "lineage")))
    identity = f"{seal}|{run_id}|{manifest_sha}|{lineage_sha}"
    signature = hmac.new(
        APPROVAL_KEY,
        f"{identity}|{approval_id}|stage9-one-shot".encode(),
        hashlib.sha256,
    ).hexdigest()
    execution_signature = hmac.new(
        APPROVAL_KEY,
        f"{identity}|{approval_id}|{commit}|{tree}|stage9-one-shot-execution".encode(),
        hashlib.sha256,
    ).hexdigest()
    path.write_text(
        json.dumps(
            {
                "status": "approved",
                "approval_id": approval_id,
                "sealed_protocol_sha256": seal,
                "robustness_release": run_id,
                "robustness_manifest_sha256": manifest_sha,
                "robustness_lineage_sha256": lineage_sha,
                "signature": signature,
                "approved_git_commit": commit,
                "approved_git_tree": tree,
                "execution_signature": execution_signature,
            }
        ),
        encoding="utf-8",
    )


def _fixture(tmp_path: Path, *, successor: bool = True) -> dict[str, Path]:
    code_root = tmp_path / "code"
    (code_root / "src").mkdir(parents=True)
    (code_root / "configs").mkdir()
    (code_root / "docs/templates").mkdir(parents=True)
    (code_root / "src/frozen_research.py").write_text("VALUE = 1\n", encoding="utf-8")
    shutil.copy2(
        Path("configs/robustness_protocol.yaml"),
        code_root / "configs/robustness_protocol.yaml",
    )
    shutil.copy2(
        Path("configs/market_rules.yaml"), code_root / "configs/market_rules.yaml"
    )
    shutil.copy2(
        Path("configs/final_execution_sources.yaml"),
        code_root / "configs/final_execution_sources.yaml",
    )
    shutil.copy2(
        Path("configs/research_protocol.yaml"),
        code_root / "configs/research_protocol.yaml",
    )
    shutil.copy2(
        Path("docs/templates/stage9-final-test-report-template.md"),
        code_root / "docs/templates/stage9-final-test-report-template.md",
    )
    _git(code_root, "init")
    _git(code_root, "config", "user.email", "test@example.invalid")
    _git(code_root, "config", "user.name", "Test User")
    _commit_all(code_root, "sealed code")

    validation_root = tmp_path / "validation"
    validation_staging = _staging(tmp_path / "validation-staging", artifact="report.md")
    validation = publish_release(
        validation_root,
        run_id="f5e18d9_stage7_validation_shsz_successor",
        staged_datasets=validation_staging[0],
        staged_artifacts=validation_staging[1],
        lineage={"stage": "validation"},
    )

    protocol = load_robustness_protocol(code_root / "configs/robustness_protocol.yaml")
    sealed_staging = tmp_path / "robustness-staging"
    datasets = sealed_staging / "datasets"
    artifacts = sealed_staging / "artifacts"
    datasets.mkdir(parents=True)
    artifacts.mkdir()
    (datasets / "robustness_results.bin").write_bytes(b"robustness")
    audit = artifacts / "action_coverage_audit"
    audit.mkdir()
    audit_records = []
    for name, role, content in (
        ("summary.json", "action_coverage_summary", b'{"status":"ready"}\n'),
        ("query_coverage.parquet", "action_query_coverage", b"coverage"),
        ("event_differences.parquet", "action_event_differences", b"diff"),
        (
            "official_evidence_index.parquet",
            "official_action_evidence_index",
            b"evidence",
        ),
    ):
        path = audit / name
        path.write_bytes(content)
        audit_records.append(file_record(path, root=audit, role=role).to_dict())
    write_json(audit / "manifest.json", {"status": "ready", "files": audit_records})
    successor_args = (
        {
            "action_source_contract_sha256": sha256_file(
                code_root / "configs/final_execution_sources.yaml"
            ),
            "action_coverage_audit_sha256": sha256_file(audit / "manifest.json"),
            "predecessor": {
                "run_id": "58adad4_stage8_robustness",
                "manifest_sha256": "f" * 64,
                "reason": "final execution coverage incomplete",
                "status": "superseded_for_final_execution",
            },
        }
        if successor
        else {}
    )
    sealed = seal_test_protocol(
        artifacts / "sealed_test_protocol.json",
        protocol=protocol,
        code_identity=code_identity(code_root),
        validation_pointer={
            "run_id": validation.run_id,
            "manifest_sha256": validation.manifest_sha256,
        },
        market_rules_sha256=hashlib.sha256(
            (code_root / "configs/market_rules.yaml").read_bytes()
        ).hexdigest(),
        report_template_sha256=hashlib.sha256(
            (
                code_root / "docs/templates/stage9-final-test-report-template.md"
            ).read_bytes()
        ).hexdigest(),
        opening_ledger_root=tmp_path / "opening-ledger",
        gate={"sealed_test_protocol_allowed": True, "status": "ready_to_seal"},
        **successor_args,
    )
    robustness_root = tmp_path / "robustness"
    lineage = {
        "stage": "robustness",
        "inputs": {
            "research_protocol": {
                "sha256": hashlib.sha256(
                    (code_root / "configs/research_protocol.yaml").read_bytes()
                ).hexdigest(),
                "size": (code_root / "configs/research_protocol.yaml").stat().st_size,
            }
        },
    }
    if successor:
        lineage["predecessor"] = successor_args["predecessor"]
        lineage["execution_protocol"] = {
            "action_source_contract_sha256": successor_args[
                "action_source_contract_sha256"
            ],
            "action_coverage_audit_sha256": successor_args[
                "action_coverage_audit_sha256"
            ],
            "supported_markets": ["sh", "sz"],
            "status": "ready_for_new_final_test_authorization",
        }
    robustness = publish_release(
        robustness_root,
        run_id="58adad4_stage8_robustness",
        staged_datasets=datasets,
        staged_artifacts=artifacts,
        lineage=lineage,
    )
    token = tmp_path / "opening-token.json"
    _write_token(token, sealed["sealed_protocol_sha256"], code_root, robustness)
    return {
        "code": code_root,
        "validation": validation_root,
        "robustness": robustness_root,
        "registry": tmp_path / "attempt-registry",
        "token": token,
        "opening_ledger": tmp_path / "opening-ledger",
    }


@pytest.mark.parametrize("changed", ["manifest", "lineage"])
def test_authorization_detects_concurrent_stage8_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    paths = _fixture(tmp_path)
    original = gate_module.save_token_snapshot

    def mutate_after_snapshot(*args: object, **kwargs: object) -> Path:
        snapshot = original(*args, **kwargs)
        current = resolve_current(paths["robustness"])
        getattr(current, changed).write_text('{"changed":true}\n', encoding="utf-8")
        return snapshot

    monkeypatch.setattr(gate_module, "save_token_snapshot", mutate_after_snapshot)
    with pytest.raises(ValueError, match="identity changed"):
        authorize_final_test(
            code_root=paths["code"], robustness_root=paths["robustness"],
            validation_root=paths["validation"], opening_token_path=paths["token"],
            approval_key=APPROVAL_KEY, registry_root=paths["registry"],
            requested_start=date(2022, 1, 1), requested_end=date(2025, 12, 31),
            attempt_id="concurrent-drift",
        )
    outcome = json.loads(
        (paths["registry"] / "concurrent-drift.outcome.json").read_text()
    )
    assert outcome["status"] == "failed"
    assert outcome["authoritative"] is False


def _republish_with_sealed_mutation(
    paths: dict[str, Path],
    tmp_path: Path,
    mutate: Callable[[dict[str, object]], None],
) -> None:
    current = resolve_current(paths["robustness"])
    staging = tmp_path / "mutated-robustness"
    shutil.copytree(current.datasets, staging / "datasets")
    shutil.copytree(current.artifacts, staging / "artifacts")
    sealed_path = staging / "artifacts/sealed_test_protocol.json"
    sealed = json.loads(sealed_path.read_text(encoding="utf-8"))
    mutate(sealed)
    sealed.pop("sealed_protocol_sha256", None)
    canonical = json.dumps(
        sealed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    sealed["sealed_protocol_sha256"] = hashlib.sha256(canonical).hexdigest()
    sealed_path.write_text(
        json.dumps(sealed, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lineage = json.loads(current.lineage.read_text(encoding="utf-8"))
    publish_release(
        paths["robustness"],
        run_id="mutated_runtime",
        staged_datasets=staging / "datasets",
        staged_artifacts=staging / "artifacts",
        lineage=lineage,
    )
    _write_token(
        paths["token"], sealed["sealed_protocol_sha256"], paths["code"],
        resolve_current(paths["robustness"]),
    )


def _authorize(paths: dict[str, Path], **overrides: object):
    arguments = {
        "code_root": paths["code"],
        "robustness_root": paths["robustness"],
        "validation_root": paths["validation"],
        "opening_token_path": paths["token"],
        "approval_key": APPROVAL_KEY,
        "registry_root": paths["registry"],
        "requested_start": date(2022, 1, 1),
        "requested_end": date(2025, 12, 31),
        "attempt_id": "attempt-001",
    }
    arguments.update(overrides)
    return authorize_final_test(**arguments)


def test_authorization_registers_attempt_before_returning(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)

    authorization = _authorize(paths)

    records = list(paths["registry"].glob("*.json"))
    assert authorization.attempt_id == "attempt-001"
    assert authorization.git_commit == _git(paths["code"], "rev-parse", "HEAD")
    assert authorization.git_tree == _git(paths["code"], "rev-parse", "HEAD^{tree}")
    assert authorization.test_period == (date(2022, 1, 1), date(2025, 12, 31))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["attempt_id"] == authorization.attempt_id
    assert record["registered_at"] == authorization.registered_at
    assert record["git_commit"] == authorization.git_commit
    assert record["git_tree"] == authorization.git_tree
    assert record["token_sha256"] == hashlib.sha256(
        paths["token"].read_bytes()
    ).hexdigest()
    assert record["sealed_protocol_sha256"] == authorization.sealed_protocol_sha256
    assert record["status"] == "registered"


def test_authorization_rejects_legacy_v1_seal(tmp_path: Path) -> None:
    paths = _fixture(tmp_path, successor=False)

    with pytest.raises(ValueError, match="successor protocol v2"):
        _authorize(paths)
    assert not paths["registry"].exists()


def test_missing_user_approval_is_rejected_without_attempt(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    paths["token"].unlink()

    with pytest.raises(ValueError, match="opening token"):
        _authorize(paths)

    assert not paths["registry"].exists()


@pytest.mark.parametrize(
    ("relative_path", "replacement", "message"),
    [
        ("src/frozen_research.py", "VALUE = 2\n", "frozen code"),
        (
            "configs/robustness_protocol.yaml",
            None,
            "robustness protocol hash",
        ),
        ("configs/market_rules.yaml", None, "market rules hash"),
        (
            "docs/templates/stage9-final-test-report-template.md",
            "changed template\n",
            "report template hash",
        ),
    ],
)
def test_frozen_hash_change_is_rejected_without_refreezing(
    tmp_path: Path,
    relative_path: str,
    replacement: str | None,
    message: str,
) -> None:
    paths = _fixture(tmp_path)
    target = paths["code"] / relative_path
    if relative_path.endswith("robustness_protocol.yaml"):
        replacement = target.read_text(encoding="utf-8").replace(
            "random_seed: 20260715", "random_seed: 20260716"
        )
    elif replacement is None:
        replacement = target.read_text(encoding="utf-8") + "\n"
    target.write_text(replacement, encoding="utf-8")
    _commit_all(paths["code"], "change sealed input")

    with pytest.raises(ValueError, match=message):
        _authorize(paths)

    assert not paths["registry"].exists()
    assert not any(paths["opening_ledger"].glob("*.json"))


def test_changed_upstream_release_is_rejected(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    staging = _staging(tmp_path / "replacement-validation", artifact="report.md")
    publish_release(
        paths["validation"],
        run_id="replacement_validation",
        staged_datasets=staging[0],
        staged_artifacts=staging[1],
        lineage={"stage": "validation"},
    )

    with pytest.raises(ValueError, match="upstream validation release"):
        _authorize(paths)


def test_research_protocol_mutation_is_rejected_from_robustness_lineage(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    research_protocol = paths["code"] / "configs/research_protocol.yaml"
    research_protocol.write_text(
        research_protocol.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    _commit_all(paths["code"], "mutate research protocol")

    with pytest.raises(ValueError, match="research protocol"):
        _authorize(paths)

    assert not paths["registry"].exists()
    assert not any(paths["opening_ledger"].glob("*.json"))


def test_approval_is_bound_to_exact_execution_commit_and_tree(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    (paths["code"] / "execution-note.txt").write_text("new tree\n", encoding="utf-8")
    _commit_all(paths["code"], "change execution tree")

    with pytest.raises(ValueError, match="execution Git commit/tree"):
        _authorize(paths)

    assert not paths["registry"].exists()
    assert not any(paths["opening_ledger"].glob("*.json"))


def test_sealed_runtime_python_and_dependencies_are_verified(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)

    def change_runtime(sealed: dict[str, object]) -> None:
        code = dict(sealed["code"])
        code["python"] = "0.0.0"
        sealed["code"] = code

    _republish_with_sealed_mutation(paths, tmp_path, change_runtime)

    with pytest.raises(ValueError, match="runtime Python"):
        _authorize(paths)

    assert not paths["registry"].exists()


def test_sealed_metrics_cannot_substitute_robustness_metric_list(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)

    def substitute_robustness_metrics(sealed: dict[str, object]) -> None:
        sealed["metrics"] = [
            "annual_return",
            "annual_volatility",
            "maximum_drawdown",
            "turnover",
            "cost_erosion",
            "unfilled_rate",
            "target_deviation",
        ]

    _republish_with_sealed_mutation(paths, tmp_path, substitute_robustness_metrics)

    with pytest.raises(ValueError, match="frozen metrics"):
        _authorize(paths)

    assert not paths["registry"].exists()


def test_token_is_read_once_and_snapshot_is_consumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _fixture(tmp_path)
    original_read_bytes = Path.read_bytes
    reads = 0

    def counted_read_bytes(path: Path) -> bytes:
        nonlocal reads
        if path == paths["token"]:
            reads += 1
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)

    _authorize(paths)

    assert reads == 1
    assert (paths["registry"] / "attempt-001.token").is_file()


def test_attempt_collision_does_not_consume_token(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    paths["registry"].mkdir()
    existing = paths["registry"] / "attempt-001.json"
    existing.write_text('{"attempt_id":"attempt-001","status":"failed"}\n')

    with pytest.raises(ValueError, match="already registered"):
        _authorize(paths)

    assert json.loads(existing.read_text())["status"] == "failed"
    assert not any(paths["opening_ledger"].glob("*.json"))


def test_consume_failure_preserves_registered_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _fixture(tmp_path)

    def fail_consume(*args: object, **kwargs: object) -> object:
        raise ValueError("injected consume failure")

    monkeypatch.setattr(
        "ashare_multifactor.final_test.gate.verify_test_opening_token",
        fail_consume,
    )

    with pytest.raises(ValueError, match="injected consume failure"):
        _authorize(paths)

    record = json.loads(
        (paths["registry"] / "attempt-001.json").read_text(encoding="utf-8")
    )
    assert record["status"] == "registered"
    assert (paths["registry"] / "attempt-001.token").is_file()


def test_out_of_range_date_is_rejected_before_approval_consumption(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)

    with pytest.raises(ValueError, match="final-test period"):
        _authorize(paths, requested_end=date(2026, 1, 1))

    assert not paths["registry"].exists()
    assert not any(paths["opening_ledger"].glob("*.json"))


def test_existing_authoritative_success_blocks_another_attempt(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    paths["registry"].mkdir()
    (paths["registry"] / "completed.json").write_text(
        json.dumps(
            {
                "attempt_id": "completed",
                "status": "succeeded",
                "authoritative": True,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="authoritative final-test run already succeeded"):
        _authorize(paths)

    assert not any(paths["opening_ledger"].glob("*.json"))
