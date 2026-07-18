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
from ashare_multifactor.final_test import preparation as preparation_module
from ashare_multifactor.final_test import resume as resume_module
from ashare_multifactor.final_test.final_root_binding import FinalRootBinding
from ashare_multifactor.final_test.preparation import _publish_preparation, verify_preparation
from ashare_multifactor.final_test.official_query_coverage import OfficialQueryScope
from ashare_multifactor.final_test.registry import (
    append_attempt_state,
)
from test_final_test_official_query_index import _write_index
from ashare_multifactor.final_test.resume import (
    load_registered_authorization,
    preflight_resume,
    verify_resume_coverages,
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


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


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
        "ashare_multifactor.final_test.preparation.resolve_final_test_data_panel_at",
        lambda _root, _fd: resolution,
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.preparation.validate_panel_source",
        lambda _root, _period: SimpleNamespace(files=(partition,)),
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.preparation._symbols_from_verified_panel_at",
        lambda _fd: symbols,
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


def test_preparation_rejects_lock_removed_after_authorization_without_recreating_it(
    prepared_attempt: PreparedAttempt, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = prepared_attempt.data_root / "processed/final_test/attempts"
    lock = registry / f"{prepared_attempt.attempt_id}.lock"
    original_load = resume_module.load_registered_authorization
    after_removal: dict[str, bytes] = {}

    def load_then_remove_lock(*args: object, **kwargs: object):
        authorization = original_load(*args, **kwargs)
        lock.unlink()
        after_removal.update(_registry_snapshot(prepared_attempt))
        return authorization

    monkeypatch.setattr(
        resume_module, "load_registered_authorization", load_then_remove_lock
    )

    with pytest.raises(ValueError, match="lock"):
        _preflight(prepared_attempt)

    assert _registry_snapshot(prepared_attempt) == after_removal
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


def test_bound_preparation_verification_uses_held_root_during_transient_replacement(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A binding must not fall back to the lexical final-test name mid-read."""
    final_root = prepared_attempt.data_root / "processed/final_test"
    resolution = SimpleNamespace(
        root=final_root / "daily_panel",
        claim_status="published",
        requires_recovery=False,
        data_manifest_sha256=hashlib.sha256(
            (final_root / "daily_panel/data_manifest.json").read_bytes()
        ).hexdigest(),
    )
    displaced = tmp_path / "held-final-root"
    replacement_before: dict[str, bytes] | None = None
    replacement_after: dict[str, bytes] | None = None
    attacked = False
    restored = False

    def restore() -> None:
        nonlocal replacement_after, restored
        if restored or not attacked:
            return
        replacement_after = _tree_bytes(final_root)
        shutil.rmtree(final_root)
        displaced.rename(final_root)
        restored = True

    def resolve_from_held_root(_root: Path, final_fd: int) -> SimpleNamespace:
        assert final_fd >= 0
        return resolution

    original_scope = getattr(preparation_module, "_verify_symbol_scope_at", None)

    def scope_from_held_root(preparation_fd: int, record: object) -> object:
        nonlocal attacked, replacement_before
        assert callable(original_scope)
        attacked = True
        final_root.rename(displaced)
        shutil.copytree(displaced, final_root)
        (
            final_root
            / "preparations"
            / prepared_attempt.attempt_id
            / "symbol_scope.parquet"
        ).write_bytes(b"replacement scope must never be read\n")
        replacement_before = _tree_bytes(final_root)
        try:
            return original_scope(preparation_fd, record)
        finally:
            restore()

    def symbols_from_held_panel(_panel_fd: int) -> list[str]:
        try:
            return ["000001", "600000"]
        finally:
            restore()

    monkeypatch.setattr(
        preparation_module,
        "resolve_final_test_data_panel_at",
        resolve_from_held_root,
        raising=False,
    )
    monkeypatch.setattr(
        preparation_module,
        "_verify_symbol_scope_at",
        scope_from_held_root,
        raising=False,
    )
    monkeypatch.setattr(
        preparation_module, "_symbols_from_verified_panel_at", symbols_from_held_panel, raising=False
    )

    with FinalRootBinding.open(final_root) as binding:
        try:
            authorization = load_registered_authorization(
                code_root=prepared_attempt.code_root,
                data_root=prepared_attempt.data_root,
                attempt_id=prepared_attempt.attempt_id,
                approval_key=prepared_attempt.approval_key,
                root_binding=binding,
            )
            result = verify_preparation(
                final_root,
                attempt_id=prepared_attempt.attempt_id,
                authorization=authorization,
                root_binding=binding,
            )
        finally:
            restore()

    assert attacked
    assert replacement_before == replacement_after
    assert result.symbols_sha256 == hashlib.sha256(b"000001\n600000\n").hexdigest()


def test_bound_coverage_revalidation_uses_held_scope_during_transient_replacement(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The post-claim check must not reload a replacement symbol scope by path."""
    preflight = _preflight(prepared_attempt)
    final_root = prepared_attempt.data_root / "processed/final_test"
    displaced = tmp_path / "held-final-root"
    replacement_before: dict[str, bytes] | None = None
    replacement_after: dict[str, bytes] | None = None
    attacked = False
    original = resume_module.load_bound_symbol_scope_at

    def load_from_held_root(final_fd: int, preparation: object) -> list[str]:
        nonlocal attacked, replacement_before, replacement_after
        attacked = True
        final_root.rename(displaced)
        shutil.copytree(displaced, final_root)
        (
            final_root
            / "preparations"
            / prepared_attempt.attempt_id
            / "symbol_scope.parquet"
        ).write_bytes(b"replacement scope must never be read\n")
        replacement_before = _tree_bytes(final_root)
        try:
            return original(final_fd, preparation)  # type: ignore[arg-type]
        finally:
            replacement_after = _tree_bytes(final_root)
            shutil.rmtree(final_root)
            displaced.rename(final_root)

    monkeypatch.setattr(resume_module, "load_bound_symbol_scope_at", load_from_held_root)

    with FinalRootBinding.open(final_root) as binding:
        verify_resume_coverages(
            preflight,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            root_binding=binding,
        )

    assert attacked
    assert replacement_before == replacement_after


def test_bound_authorization_does_not_lexically_validate_final_root(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The held binding is the final-root authority, not a later path check."""
    def fail_on_lexical_final_root(*_args: object, **_kwargs: object) -> None:
        pytest.fail("bound authorization revalidated final-test root by path")

    monkeypatch.setattr(resume_module, "_verify_data_paths", fail_on_lexical_final_root)
    final_root = prepared_attempt.data_root / "processed/final_test"
    displaced = tmp_path / "held-final-root"
    replacement_before: dict[str, bytes] | None = None
    replacement_after: dict[str, bytes] | None = None
    with FinalRootBinding.open(final_root) as binding:
        original_assert_bound = FinalRootBinding.assert_bound
        original_resolve_state = resume_module.resolve_attempt_state_at
        replaced = False

        def replace_after_initial_assert(current: FinalRootBinding) -> None:
            nonlocal replaced, replacement_before
            original_assert_bound(current)
            if current is not binding or replaced:
                return
            replaced = True
            final_root.rename(displaced)
            shutil.copytree(displaced, final_root)
            replacement = final_root / "attempts" / f"{prepared_attempt.attempt_id}.json"
            replacement.unlink()
            replacement.symlink_to(tmp_path / "replacement-registration.json")
            (tmp_path / "replacement-registration.json").write_bytes(
                b"replacement registration must never be read\n"
            )
            replacement_before = _tree_bytes(final_root)

        def resolve_from_held_registry(registry_fd: int, attempt_id: str) -> dict[str, object]:
            nonlocal replacement_after
            try:
                return original_resolve_state(registry_fd, attempt_id)
            finally:
                replacement_after = _tree_bytes(final_root)
                shutil.rmtree(final_root)
                displaced.rename(final_root)

        monkeypatch.setattr(FinalRootBinding, "assert_bound", replace_after_initial_assert)
        monkeypatch.setattr(resume_module, "resolve_attempt_state_at", resolve_from_held_registry)
        try:
            authorization = load_registered_authorization(
                code_root=prepared_attempt.code_root,
                data_root=prepared_attempt.data_root,
                attempt_id=prepared_attempt.attempt_id,
                approval_key=prepared_attempt.approval_key,
                root_binding=binding,
            )
        finally:
            if displaced.exists():
                replacement_after = _tree_bytes(final_root)
                shutil.rmtree(final_root)
                displaced.rename(final_root)

    assert authorization.attempt_id == prepared_attempt.attempt_id
    assert replaced
    assert replacement_before == replacement_after


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
def test_preflight_uses_validator_hash_if_manifest_changes_after_validation(
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
    validated_hashes: list[str] = []

    def mutate_after_validate(*args: object, **kwargs: object):
        verified = original_validate(*args, **kwargs)
        validated_hash = verified["coverage_manifest_sha256"]
        assert isinstance(validated_hash, str)
        validated_hashes.append(validated_hash)
        manifest = verified["coverage_manifest_path"]
        assert isinstance(manifest, Path)
        manifest.write_text('{"changed":true}\n', encoding="utf-8")
        return verified

    monkeypatch.setattr(execution_sources_module, attribute, mutate_after_validate)

    result = _preflight(prepared_attempt)

    result_hash = (
        result.security_event_coverage_sha256
        if coverage == "security"
        else result.corporate_action_coverage_sha256
    )
    assert result_hash == validated_hashes[0]


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


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_event_type", "security event.*schema"),
        ("null_effective_date", "security event.*contract"),
        ("nan_ratio", "security event.*contract"),
        ("infinite_cash", "security event.*contract"),
        ("duplicate_key", "security event.*duplicate|security event.*contract"),
        ("self_merger", "security event.*contract"),
    ],
)
def test_preflight_rejects_invalid_security_execution_rows_before_claim(
    prepared_attempt: PreparedAttempt,
    mutation: str,
    message: str,
) -> None:
    rows = 2 if mutation == "duplicate_key" else 1
    events = pl.DataFrame(
        {
            "source_symbol": ["000001"] * rows,
            "effective_date": [date(2023, 6, 5)] * rows,
            "event_type": ["stock_merger"] * rows,
            "target_symbol": ["000002"] * rows,
            "ratio": [1.0] * rows,
            "cash_per_share": [0.0] * rows,
            "source": ["szse"] * rows,
            "evidence_id": ["ev-sz"] * rows,
        }
    )
    if mutation == "missing_event_type":
        events = events.drop("event_type")
    elif mutation == "null_effective_date":
        events = events.with_columns(
            pl.lit(None, dtype=pl.Date).alias("effective_date")
        )
    elif mutation == "nan_ratio":
        events = events.with_columns(pl.lit(float("nan")).alias("ratio"))
    elif mutation == "infinite_cash":
        events = events.with_columns(
            pl.lit(float("inf")).alias("cash_per_share")
        )
    elif mutation == "self_merger":
        events = events.with_columns(
            pl.col("source_symbol").alias("target_symbol")
        )
    _set_security_events(prepared_attempt, events)
    before = _registry_snapshot(prepared_attempt)

    with pytest.raises(ValueError, match=message):
        _preflight(prepared_attempt)

    assert _registry_snapshot(prepared_attempt) == before
    assert _attempt_state(prepared_attempt) == "awaiting_official_evidence"
    final_root = prepared_attempt.data_root / "processed/final_test"
    assert not (
        final_root
        / "attempts"
        / f"{prepared_attempt.attempt_id}.outcome.json"
    ).exists()
    assert not (
        final_root / "attempt_runs" / prepared_attempt.attempt_id
    ).exists()


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
    shutil.rmtree(root / "packages", ignore_errors=True)
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
    official_query_coverage = _write_index(
        root,
        [
            OfficialQueryScope(
                symbol=symbol,
                market="sz" if symbol.startswith(("0", "3")) else "sh",
                category="security_events",
                query_category="",
                start=date(2022, 1, 1),
                end=date(2025, 12, 31),
            )
            for symbol in sorted(symbols)
        ],
    )
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
                "official_query_coverage": file_record(
                    official_query_coverage,
                    root=root,
                    role="official_query_coverage",
                ).to_dict(),
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


def _set_security_events(attempt: PreparedAttempt, events: pl.DataFrame) -> None:
    root = attempt.security_coverage.parent
    event_path = root / "security_events.parquet"
    events.write_parquet(event_path)
    coverage_path = root / "query_coverage.parquet"
    counts = events.group_by("source_symbol").len().rename(
        {"source_symbol": "symbol", "len": "new_count"}
    )
    pl.read_parquet(coverage_path).join(
        counts, on="symbol", how="left"
    ).with_columns(
        pl.col("new_count").fill_null(0).cast(pl.Int64).alias("event_count")
    ).drop("new_count").write_parquet(coverage_path)
    payload = json.loads(attempt.security_coverage.read_text(encoding="utf-8"))
    payload["event_rows"] = events.height
    payload["events_file"] = file_record(
        event_path,
        root=root,
        role="official_security_events",
    ).to_dict()
    payload["coverage"] = [
        file_record(
            coverage_path,
            root=root,
            role="official_security_event_coverage",
        ).to_dict()
    ]
    attempt.security_coverage.write_text(json.dumps(payload), encoding="utf-8")
