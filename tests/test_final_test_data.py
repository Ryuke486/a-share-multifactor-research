from dataclasses import replace
from datetime import date
import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess

import polars as pl
import pytest

from test_build import _config, _write_pair

from ashare_multifactor.audit.publication import publish_release
from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.data.build import build_parquet_dataset
from ashare_multifactor.final_test.data_extension import build_final_test_daily_panel
from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.final_test.registry import register_attempt, save_token_snapshot


FINAL_START = date(2022, 1, 1)
FINAL_END = date(2025, 12, 31)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _publish_robustness(config, tmp_path: Path, *, run_id: str, seal: str) -> object:
    staging = tmp_path / f"{run_id}-staging"
    datasets = staging / "datasets"
    artifacts = staging / "artifacts"
    datasets.mkdir(parents=True)
    artifacts.mkdir()
    (datasets / "results.bin").write_bytes(run_id.encode())
    (artifacts / "sealed_test_protocol.json").write_text(
        json.dumps({"sealed_protocol_sha256": seal}) + "\n",
        encoding="utf-8",
    )
    return publish_release(
        config.paths.processed / "robustness",
        run_id=run_id,
        staged_datasets=datasets,
        staged_artifacts=artifacts,
        lineage={"stage": "robustness"},
    )


def _authorized_context(tmp_path: Path, config) -> tuple[Path, FinalTestAuthorization]:
    code_root = tmp_path / "code"
    code_root.mkdir()
    (code_root / "frozen.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(code_root, "init")
    _git(code_root, "config", "user.email", "test@example.invalid")
    _git(code_root, "config", "user.name", "Test User")
    _git(code_root, "add", ".")
    _git(code_root, "commit", "-m", "authorized code")
    commit = _git(code_root, "rev-parse", "HEAD")
    tree = _git(code_root, "rev-parse", "HEAD^{tree}")

    seal = "c" * 64
    robustness = _publish_robustness(
        config,
        tmp_path,
        run_id="stage8-release",
        seal=seal,
    )
    token_bytes = b'{"fixture":"opening-token"}\n'
    token_sha256 = hashlib.sha256(token_bytes).hexdigest()
    registry_root = config.paths.processed / "final_test/attempts"
    record = register_attempt(
        registry_root,
        attempt_id="attempt-001",
        git_commit=commit,
        git_tree=tree,
        token_sha256=token_sha256,
        sealed_protocol_sha256=seal,
        robustness_release="stage8-release",
        approval_id="approved-stage9",
        robustness_manifest_sha256=str(getattr(robustness, "manifest_sha256")),
        robustness_lineage_sha256=sha256_file(Path(getattr(robustness, "lineage"))),
    )
    save_token_snapshot(
        registry_root,
        attempt_id="attempt-001",
        token_bytes=token_bytes,
        expected_sha256=token_sha256,
    )
    authorization = FinalTestAuthorization(
        attempt_id="attempt-001",
        approval_id="approved-stage9",
        registered_at=str(record["registered_at"]),
        git_commit=commit,
        git_tree=tree,
        sealed_protocol_sha256=seal,
        robustness_release="stage8-release",
        test_period=(FINAL_START, FINAL_END),
        robustness_manifest_sha256=str(getattr(robustness, "manifest_sha256")),
        robustness_lineage_sha256=sha256_file(Path(getattr(robustness, "lineage"))),
    )
    return code_root, authorization


def _build(config, authorization: FinalTestAuthorization, code_root: Path):
    return build_final_test_daily_panel(
        config,
        authorization,
        FINAL_START,
        FINAL_END,
        code_root=code_root,
    )


def _raw_hashes(config) -> dict[str, str]:
    roots = (config.paths.raw_unadjusted, config.paths.raw_backward_adjusted)
    return {
        path.as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for root in roots
        for path in sorted(root.glob("*.csv"))
    }


def _forbid_discovery(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        calls.append("called")
        raise AssertionError("discovery must not run")

    monkeypatch.setattr(
        "ashare_multifactor.final_test.data_extension.discover_daily_pairs",
        forbidden,
    )
    monkeypatch.setattr("ashare_multifactor.data.build.discover_daily_pairs", forbidden)
    return calls


def test_final_test_builder_requires_authorization_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    calls = _forbid_discovery(monkeypatch)

    with pytest.raises(TypeError, match="FinalTestAuthorization"):
        build_final_test_daily_panel(
            config,
            None,
            FINAL_START,
            FINAL_END,
            code_root=tmp_path / "code",
        )

    assert calls == []


@pytest.mark.parametrize(
    ("authorization_period", "start", "end", "message"),
    [
        (
            (FINAL_START, date(2025, 12, 30)),
            FINAL_START,
            FINAL_END,
            "authorization period",
        ),
        (
            (FINAL_START, FINAL_END),
            date(2022, 1, 2),
            FINAL_END,
            "exact final-test period",
        ),
        (
            (FINAL_START, FINAL_END),
            FINAL_START,
            date(2026, 1, 2),
            "exact final-test period",
        ),
    ],
)
def test_final_test_builder_rejects_period_changes_before_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authorization_period: tuple[date, date],
    start: date,
    end: date,
    message: str,
) -> None:
    config = _config(tmp_path)
    code_root, authorization = _authorized_context(tmp_path, config)
    authorization = replace(authorization, test_period=authorization_period)
    calls = _forbid_discovery(monkeypatch)

    with pytest.raises(ValueError, match=message):
        build_final_test_daily_panel(
            config,
            authorization,
            start,
            end,
            code_root=code_root,
        )

    assert calls == []


def test_final_test_builder_reuses_canonical_schema_and_manifest_without_mutating_raw(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    days = (
        date(2022, 1, 3),
        date(2023, 1, 3),
        date(2024, 1, 2),
        date(2025, 12, 31),
    )
    for day in days:
        _write_pair(config, day)
    raw_before = _raw_hashes(config)
    code_root, authorization = _authorized_context(tmp_path, config)

    manifest = _build(config, authorization, code_root)

    root = config.paths.processed / "final_test/daily_panel"
    saved = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    panel = pl.concat(
        pl.read_parquet(path) for path in sorted(root.glob("year=*/part-000.parquet"))
    )
    assert manifest.file_pairs == 4
    assert manifest.rows == 4
    assert manifest.min_date == days[0]
    assert manifest.max_date == days[-1]
    assert manifest.years == (2022, 2023, 2024, 2025)
    assert panel.select("date", "symbol").rows() == [
        (day, "000001") for day in days
    ]
    assert saved == manifest.to_dict()
    assert saved["quality_issues"]["records"] == 0
    assert saved["quality_issues"]["sha256"] == hashlib.sha256(
        (root / "quality_issues.json").read_bytes()
    ).hexdigest()
    for partition in saved["partitions"]:
        path = root / partition["relative_path"]
        assert partition["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert partition["rows"] == 1
        assert partition["min_date"] == partition["max_date"]
    inventory = json.loads((root / "input_files.json").read_text(encoding="utf-8"))
    assert inventory["schema_version"] == "1"
    assert inventory["period"] == ["2022-01-01", "2025-12-31"]
    assert [pair["trade_date"] for pair in inventory["pairs"]] == [
        day.isoformat() for day in days
    ]
    files = [item for pair in inventory["pairs"] for item in pair["files"]]
    assert len(files) == 8
    assert {item["role"] for item in files} == {
        "unadjusted",
        "backward_adjusted",
    }
    assert all(item["relative_path"].endswith("_金玥数据.csv") for item in files)
    assert all(item["rows"] == 1 for item in files)
    assert all(item["size_bytes"] > 0 for item in files)
    assert all(len(item["sha256"]) == 64 for item in files)
    claim = json.loads(
        (config.paths.processed / "final_test/data-build-claim.json").read_text()
    )
    assert claim["attempt_id"] == authorization.attempt_id
    assert claim["robustness_manifest_sha256"] == authorization.robustness_manifest_sha256
    assert claim["robustness_lineage_sha256"] == authorization.robustness_lineage_sha256
    assert claim["status"] == "published"
    assert claim["data_manifest"]["relative_path"] == "daily_panel/data_manifest.json"
    assert _raw_hashes(config) == raw_before
    assert not (config.paths.processed / "validation_evaluation").exists()


def test_final_test_builder_excludes_bse_before_publishing_panel(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    day = date(2022, 1, 3)
    _write_pair(config, day)
    raw_path = next(config.paths.raw_unadjusted.glob("*.csv"))
    adjusted_path = next(config.paths.raw_backward_adjusted.glob("*.csv"))
    raw_lines = raw_path.read_text(encoding="utf-8").splitlines()
    adjusted_lines = adjusted_path.read_text(encoding="utf-8").splitlines()
    raw_path.write_text(
        "\n".join([raw_lines[0], raw_lines[1], raw_lines[1].replace("000001", "920001", 1)])
        + "\n",
        encoding="utf-8",
    )
    adjusted_path.write_text(
        "\n".join(
            [
                adjusted_lines[0],
                adjusted_lines[1],
                adjusted_lines[1].replace("000001", "920001", 1),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    code_root, authorization = _authorized_context(tmp_path, config)

    manifest = _build(config, authorization, code_root)

    root = config.paths.processed / "final_test/daily_panel"
    panel = pl.read_parquet(root / "year=2022/part-000.parquet")
    issues = json.loads((root / "quality_issues.json").read_text(encoding="utf-8"))
    assert manifest.rows == 1
    assert panel.get_column("symbol").to_list() == ["000001"]
    assert issues == [
        {
            "code": "outside_supported_markets_excluded",
            "count": 1,
            "date": day.isoformat(),
            "message": (
                "rows outside the configured supported market scope were excluded"
            ),
            "severity": "warning",
            "supported_markets": ["sh", "sz"],
        }
    ]


def test_final_test_builder_keeps_stage_seven_quality_policy(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    day = date(2022, 1, 3)
    _write_pair(config, day)
    raw_path = next(config.paths.raw_unadjusted.glob("*.csv"))
    adj_path = next(config.paths.raw_backward_adjusted.glob("*.csv"))
    raw = raw_path.read_text(encoding="utf-8").splitlines()
    adjusted = adj_path.read_text(encoding="utf-8").splitlines()
    bad_raw = raw[1].replace("000001", "000002", 1).replace(
        "10.5,9.8,10.2", "9.0,9.8,10.2", 1
    )
    bad_adjusted = adjusted[1].replace("000001", "000002", 1)
    raw_path.write_text("\n".join([raw[0], bad_raw, raw[1]]) + "\n", encoding="utf-8")
    adj_path.write_text(
        "\n".join([adjusted[0], bad_adjusted, adjusted[1]]) + "\n",
        encoding="utf-8",
    )
    code_root, authorization = _authorized_context(tmp_path, config)

    manifest = _build(config, authorization, code_root)

    root = config.paths.processed / "final_test/daily_panel"
    issues = json.loads((root / "quality_issues.json").read_text(encoding="utf-8"))
    assert manifest.rows == 1
    assert manifest.quality_issues.records == 1
    assert manifest.quality_issues.quarantined_rows == 1
    assert issues == [
        {
            "code": "invalid_ohlc_quarantined",
            "count": 1,
            "date": "2022-01-03",
            "message": (
                "rows with non-positive or inconsistent raw/adjusted OHLC were quarantined"
            ),
            "severity": "warning",
            "symbols": ["000002"],
        }
    ]


def test_final_test_builder_rejects_noncanonical_configured_test_period(
    tmp_path: Path,
) -> None:
    base = _config(tmp_path)
    config = replace(
        base,
        test=replace(base.test, end=date(2026, 1, 2)),
    )
    code_root, authorization = _authorized_context(tmp_path, config)

    with pytest.raises(ValueError, match="configured test period"):
        _build(config, authorization, code_root)


def test_forged_authorization_without_canonical_attempt_is_rejected_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    code_root, authorization = _authorized_context(tmp_path, config)
    forged = replace(authorization, attempt_id="forged-attempt")
    calls = _forbid_discovery(monkeypatch)

    with pytest.raises(ValueError, match="attempt record"):
        _build(config, forged, code_root)

    assert calls == []


def test_dirty_or_changed_git_identity_is_rejected_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    code_root, authorization = _authorized_context(tmp_path, config)
    (code_root / "dirty.txt").write_text("changed\n", encoding="utf-8")
    calls = _forbid_discovery(monkeypatch)

    with pytest.raises(ValueError, match="clean Git identity"):
        _build(config, authorization, code_root)

    assert calls == []


@pytest.mark.parametrize("drift", ["release", "seal"])
def test_robustness_release_or_seal_drift_is_rejected_before_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    config = _config(tmp_path)
    code_root, authorization = _authorized_context(tmp_path, config)
    new_seal = "e" * 64 if drift == "seal" else authorization.sealed_protocol_sha256
    _publish_robustness(
        config,
        tmp_path,
        run_id="replacement-release",
        seal=new_seal,
    )
    if drift == "seal":
        authorization = replace(
            authorization,
            robustness_release="replacement-release",
        )
    calls = _forbid_discovery(monkeypatch)

    with pytest.raises(ValueError, match=drift if drift == "release" else "identity"):
        _build(config, authorization, code_root)

    assert calls == []


def test_target_overlap_and_symlink_alias_are_rejected_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _config(tmp_path)
    code_root, authorization = _authorized_context(tmp_path, base)
    calls = _forbid_discovery(monkeypatch)
    overlap = replace(
        base,
        paths=replace(
            base.paths,
            raw_unadjusted=base.paths.processed / "final_test",
        ),
    )

    with pytest.raises(ValueError, match="overlaps raw input"):
        _build(overlap, authorization, code_root)

    target = base.paths.processed / "final_test/daily_panel"
    target.symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        _build(base, authorization, code_root)

    assert calls == []


def test_exclusive_claim_blocks_second_builder_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    code_root, authorization = _authorized_context(tmp_path, config)
    claim = config.paths.processed / "final_test/data-build-claim.json"
    claim.write_text('{"status":"claimed"}\n', encoding="utf-8")
    calls = _forbid_discovery(monkeypatch)

    with pytest.raises(ValueError, match="already claimed"):
        _build(config, authorization, code_root)

    assert calls == []


def test_build_failure_is_retained_in_claim_record(tmp_path: Path) -> None:
    config = _config(tmp_path)
    code_root, authorization = _authorized_context(tmp_path, config)

    with pytest.raises(ValueError, match="no paired daily files"):
        _build(config, authorization, code_root)

    claim = json.loads(
        (config.paths.processed / "final_test/data-build-claim.json").read_text()
    )
    assert claim["attempt_id"] == authorization.attempt_id
    assert claim["status"] == "failed"
    assert claim["error_type"] == "ValueError"
    assert claim["error"] == "no paired daily files found"
    assert not (config.paths.processed / "final_test/daily_panel").exists()


def test_data_manifest_binds_stage_two_manifest_inventory_and_quality(
    tmp_path: Path,
) -> None:
    from ashare_multifactor.final_test.data_extension import (
        verify_final_test_data_panel,
    )

    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)

    _build(config, authorization, code_root)

    root = config.paths.processed / "final_test/daily_panel"
    data_manifest = verify_final_test_data_panel(root)
    stage_two = root / "manifest.json"
    inventory = root / "input_files.json"
    quality = root / "quality_issues.json"
    assert data_manifest["stage2_manifest"] == {
        "relative_path": "manifest.json",
        "sha256": hashlib.sha256(stage_two.read_bytes()).hexdigest(),
        "size_bytes": stage_two.stat().st_size,
    }
    assert data_manifest["input_files"] == {
        "relative_path": "input_files.json",
        "sha256": hashlib.sha256(inventory.read_bytes()).hexdigest(),
        "size_bytes": inventory.stat().st_size,
        "pair_count": 1,
        "file_count": 2,
    }
    assert data_manifest["quality_issues"]["relative_path"] == "quality_issues.json"
    assert data_manifest["quality_issues"]["sha256"] == hashlib.sha256(
        quality.read_bytes()
    ).hexdigest()

    inventory.write_text(
        inventory.read_text(encoding="utf-8") + " ",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="input files identity"):
        verify_final_test_data_panel(root)


def test_post_publish_claim_failure_retains_resolvable_publishing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ashare_multifactor.final_test import data_extension

    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    original_update = data_extension._update_claim

    def fail_published(*args: object, status: str, **kwargs: object) -> None:
        if status == "published":
            raise OSError("injected final claim failure")
        original_update(*args, status=status, **kwargs)

    monkeypatch.setattr(data_extension, "_update_claim", fail_published)

    with pytest.raises(RuntimeError, match="published artifact.*requires recovery"):
        _build(config, authorization, code_root)

    final_root = config.paths.processed / "final_test"
    claim = json.loads((final_root / "data-build-claim.json").read_text())
    assert claim["status"] == "publishing"
    assert "error" not in claim
    assert (final_root / "daily_panel/data_manifest.json").is_file()
    resolved = data_extension.resolve_final_test_data_panel(final_root)
    assert resolved.root == final_root / "daily_panel"
    assert resolved.claim_status == "publishing"
    assert resolved.requires_recovery is True
    assert data_extension.verify_final_test_data_panel(resolved.root)[
        "schema_version"
    ] == "1"


def test_claimed_data_build_recovers_from_bound_inventory_without_rediscovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_multifactor.final_test import data_extension

    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    monkeypatch.setattr(
        data_extension,
        "build_parquet_dataset",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            KeyboardInterrupt("hard crash after claim")
        ),
    )

    with pytest.raises(KeyboardInterrupt, match="after claim"):
        _build(config, authorization, code_root)

    claim_path = config.paths.processed / "final_test/data-build-claim.json"
    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    assert claim["status"] == "claimed"
    assert claim["input_inventory"]["relative_path"].startswith(
        "data-build-inputs/"
    )
    calls = _forbid_discovery(monkeypatch)
    monkeypatch.setattr(
        data_extension,
        "build_parquet_dataset",
        build_parquet_dataset,
    )

    recovered = data_extension.recover_final_test_daily_panel(
        config,
        authorization,
        FINAL_START,
        FINAL_END,
        code_root=code_root,
    )

    assert recovered.claim_status == "published"
    assert calls == []


def test_orphan_inventory_before_claim_recovers_without_rediscovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_multifactor.final_test import data_extension

    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    original_claim = data_extension._claim_build
    monkeypatch.setattr(
        data_extension,
        "_claim_build",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            KeyboardInterrupt("hard crash before claim publication")
        ),
    )

    with pytest.raises(KeyboardInterrupt, match="before claim publication"):
        _build(config, authorization, code_root)

    final_root = config.paths.processed / "final_test"
    inventory = final_root / f"data-build-inputs/{authorization.attempt_id}.json"
    assert inventory.is_file()
    inventory_payload = json.loads(inventory.read_text(encoding="utf-8"))
    assert inventory_payload["authorization_identity"]["attempt_id"] == (
        authorization.attempt_id
    )
    assert not (final_root / "data-build-claim.json").exists()
    inventory_bytes = inventory.read_bytes()
    monkeypatch.setattr(data_extension, "_claim_build", original_claim)
    calls = _forbid_discovery(monkeypatch)

    _build(config, authorization, code_root)

    assert calls == []
    assert inventory.read_bytes() == inventory_bytes
    assert json.loads((final_root / "data-build-claim.json").read_text())["status"] == (
        "published"
    )


@pytest.mark.parametrize(
    ("drift", "message"),
    [
        ("attempt", "inventory identity differs"),
        ("authorization", "inventory identity differs"),
        ("path", "input file"),
        ("hash", "input file identity changed"),
    ],
)
def test_orphan_inventory_mismatch_fails_closed_without_overwrite_or_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
    message: str,
) -> None:
    from ashare_multifactor.final_test import data_extension

    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    original_claim = data_extension._claim_build
    monkeypatch.setattr(
        data_extension,
        "_claim_build",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        _build(config, authorization, code_root)
    monkeypatch.setattr(data_extension, "_claim_build", original_claim)

    final_root = config.paths.processed / "final_test"
    inventory_path = (
        final_root / f"data-build-inputs/{authorization.attempt_id}.json"
    )
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    first_file = inventory["pairs"][0]["files"][0]
    if drift == "attempt":
        inventory["authorization_identity"]["attempt_id"] = "different-attempt"
        inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    elif drift == "authorization":
        inventory["authorization_identity"]["git_tree"] = "0" * 40
        inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    elif drift == "path":
        first_file["relative_path"] = "../escaped.csv"
        inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    else:
        root = {
            "unadjusted": config.paths.raw_unadjusted,
            "backward_adjusted": config.paths.raw_backward_adjusted,
        }[first_file["role"]]
        (root / first_file["relative_path"]).write_bytes(b"changed raw bytes")
    drifted_inventory_bytes = inventory_path.read_bytes()
    calls = _forbid_discovery(monkeypatch)

    with pytest.raises(ValueError, match=message):
        _build(config, authorization, code_root)

    assert calls == []
    assert inventory_path.read_bytes() == drifted_inventory_bytes
    assert not (final_root / "data-build-claim.json").exists()
    assert not (final_root / "daily_panel").exists()


def test_publishing_data_build_recovers_before_or_after_panel_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_multifactor.final_test import data_extension

    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    original_publish = data_extension.atomic_rename_no_replace_at

    def interrupt_panel_rename(
        source_fd: int,
        source: str,
        destination_fd: int,
        destination: str,
    ) -> None:
        if destination == "daily_panel":
            raise KeyboardInterrupt("hard crash before panel rename")
        original_publish(source_fd, source, destination_fd, destination)

    monkeypatch.setattr(
        data_extension, "atomic_rename_no_replace_at", interrupt_panel_rename
    )
    with pytest.raises(KeyboardInterrupt, match="before panel rename"):
        _build(config, authorization, code_root)
    claim = json.loads(
        (config.paths.processed / "final_test/data-build-claim.json").read_text()
    )
    assert claim["status"] == "publishing"
    monkeypatch.setattr(
        data_extension, "atomic_rename_no_replace_at", original_publish
    )
    calls = _forbid_discovery(monkeypatch)

    recovered = data_extension.recover_final_test_daily_panel(
        config,
        authorization,
        FINAL_START,
        FINAL_END,
        code_root=code_root,
    )

    assert recovered.claim_status == "published"
    assert calls == []


def test_post_rename_publishing_claim_is_completed_without_rediscovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_multifactor.final_test import data_extension

    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    original_update = data_extension._update_claim

    def interrupt_published(*args: object, status: str, **kwargs: object) -> None:
        if status == "published":
            raise KeyboardInterrupt("hard crash after panel rename")
        original_update(*args, status=status, **kwargs)

    monkeypatch.setattr(data_extension, "_update_claim", interrupt_published)
    with pytest.raises(KeyboardInterrupt, match="after panel rename"):
        _build(config, authorization, code_root)
    monkeypatch.setattr(data_extension, "_update_claim", original_update)
    calls = _forbid_discovery(monkeypatch)

    recovered = data_extension.recover_final_test_daily_panel(
        config,
        authorization,
        FINAL_START,
        FINAL_END,
        code_root=code_root,
    )

    assert recovered.claim_status == "published"
    assert calls == []


def test_publishing_recovery_rejects_staging_replaced_after_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_multifactor.final_test import data_extension

    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    original_rename = data_extension.atomic_rename_no_replace_at

    def interrupt_panel_rename(
        source_fd: int,
        source: str,
        destination_fd: int,
        destination: str,
    ) -> None:
        if destination == "daily_panel":
            raise KeyboardInterrupt("hard crash before panel rename")
        original_rename(source_fd, source, destination_fd, destination)

    monkeypatch.setattr(
        data_extension, "atomic_rename_no_replace_at", interrupt_panel_rename
    )
    with pytest.raises(KeyboardInterrupt):
        _build(config, authorization, code_root)

    original_publish = data_extension._publish_built_panel

    def replace_then_publish(source: Path, destination: Path, **kwargs: object) -> None:
        displaced = tmp_path / "displaced-recovery-panel"
        source.rename(displaced)
        shutil.copytree(displaced, source)
        original_publish(source, destination, **kwargs)

    monkeypatch.setattr(data_extension, "_publish_built_panel", replace_then_publish)

    with pytest.raises(ValueError, match="staging identity changed"):
        data_extension.recover_final_test_daily_panel(
            config,
            authorization,
            FINAL_START,
            FINAL_END,
            code_root=code_root,
        )

    assert not (config.paths.processed / "final_test/daily_panel").exists()


def test_data_build_rejects_staging_panel_replaced_after_stage2_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_multifactor.final_test import data_extension

    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    build_stage2 = data_extension.build_parquet_dataset

    def build_then_replace(*args: object, **kwargs: object):
        result = build_stage2(*args, **kwargs)
        target = Path(str(kwargs["output_root"]))
        displaced = tmp_path / "displaced-stage2-panel"
        target.rename(displaced)
        shutil.copytree(displaced, target)
        manifest_path = target / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["replacement_after_stage2"] = True
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return result

    monkeypatch.setattr(data_extension, "build_parquet_dataset", build_then_replace)

    with pytest.raises(ValueError, match="Stage-2 output identity changed"):
        _build(config, authorization, code_root)

    final_root = config.paths.processed / "final_test"
    assert not (final_root / "daily_panel").exists()


def test_data_build_rejects_stage2_parent_replaced_with_self_consistent_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_multifactor.final_test import data_extension

    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    build_stage2 = data_extension.build_parquet_dataset

    def build_then_replace_parent(*args: object, **kwargs: object):
        result = build_stage2(*args, **kwargs)
        target = Path(str(kwargs["output_root"]))
        parent = target.parent
        displaced = tmp_path / "displaced-stage2-parent"
        parent.rename(displaced)
        shutil.copytree(displaced, parent)
        return result

    monkeypatch.setattr(
        data_extension, "build_parquet_dataset", build_then_replace_parent
    )

    with pytest.raises(ValueError, match="output parent.*identity|staging.*identity"):
        _build(config, authorization, code_root)

    assert not (config.paths.processed / "final_test/daily_panel").exists()


def test_data_build_rejects_same_size_partition_rewrite_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_multifactor.final_test import data_extension

    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    publish = data_extension._publish_built_panel

    def rewrite_then_publish(source: Path, destination: Path, **kwargs: object) -> None:
        partition = next(source.rglob("*.parquet"))
        original = partition.read_bytes()
        changed = bytearray(original)
        changed[len(changed) // 2] ^= 1
        with partition.open("r+b") as stream:
            stream.write(changed)
        assert partition.stat().st_size == len(original)
        publish(source, destination, **kwargs)

    monkeypatch.setattr(data_extension, "_publish_built_panel", rewrite_then_publish)

    with pytest.raises(ValueError, match="staging bytes changed"):
        _build(config, authorization, code_root)

    assert not (config.paths.processed / "final_test/daily_panel").exists()


def test_data_build_cannot_succeed_after_final_root_replaced_during_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_multifactor.final_test import data_extension

    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    update_claim = data_extension._update_claim
    final_root = config.paths.processed / "final_test"

    def replace_then_update(*args: object, status: str, **kwargs: object) -> None:
        if status == "published":
            displaced = tmp_path / "displaced-final-root-after-publish"
            final_root.rename(displaced)
            shutil.copytree(displaced, final_root)
        update_claim(*args, status=status, **kwargs)

    monkeypatch.setattr(data_extension, "_update_claim", replace_then_update)

    with pytest.raises(RuntimeError, match="requires recovery/audit"):
        _build(config, authorization, code_root)


def test_atomic_identity_json_write_fsyncs_file_replace_and_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ashare_multifactor.final_test import data_inventory

    events: list[str] = []
    original_fsync = data_inventory.os.fsync
    original_replace = data_inventory.os.replace

    def tracked_fsync(descriptor: int) -> None:
        mode = data_inventory.os.fstat(descriptor).st_mode
        events.append("fsync_parent" if stat.S_ISDIR(mode) else "fsync_file")
        original_fsync(descriptor)

    def tracked_replace(source: Path, destination: Path) -> None:
        events.append("replace")
        original_replace(source, destination)

    monkeypatch.setattr(data_inventory.os, "fsync", tracked_fsync)
    monkeypatch.setattr(data_inventory.os, "replace", tracked_replace)

    data_inventory.write_json(tmp_path / "identity.json", {"value": 1})

    assert events == ["fsync_file", "replace", "fsync_parent"]


def test_initial_exclusive_claim_fsyncs_file_then_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ashare_multifactor.final_test import data_publication

    config = _config(tmp_path)
    _, authorization = _authorized_context(tmp_path, config)
    final_root = tmp_path / "claim-root"
    events: list[str] = []
    original_fsync = data_publication.os.fsync

    def tracked_fsync(descriptor: int) -> None:
        mode = data_publication.os.fstat(descriptor).st_mode
        events.append("fsync_parent" if stat.S_ISDIR(mode) else "fsync_file")
        original_fsync(descriptor)

    monkeypatch.setattr(data_publication.os, "fsync", tracked_fsync)

    data_publication.claim_build(final_root, authorization)

    assert events == ["fsync_file", "fsync_parent"]


def test_interrupted_claim_write_never_exposes_partial_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ashare_multifactor.final_test import data_publication

    config = _config(tmp_path)
    _, authorization = _authorized_context(tmp_path, config)
    final_root = tmp_path / "claim-root"

    write_bytes = data_publication.write_bytes_exclusive_at

    def interrupt_write(parent_fd: int, name: str, _payload: bytes) -> None:
        write_bytes(parent_fd, name, b'{"status":')
        raise KeyboardInterrupt("hard crash while writing claim")

    monkeypatch.setattr(
        data_publication, "write_bytes_exclusive_at", interrupt_write
    )

    with pytest.raises(KeyboardInterrupt, match="while writing claim"):
        data_publication.claim_build(final_root, authorization)

    assert not (final_root / "data-build-claim.json").exists()
    assert list(final_root.glob(".data-build-claim.*.tmp")) == []


def test_claim_publication_is_atomic_no_replace_under_competition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ashare_multifactor.final_test import data_publication

    config = _config(tmp_path)
    _, authorization = _authorized_context(tmp_path, config)
    final_root = tmp_path / "claim-root"
    final_root.mkdir()
    claim_path = final_root / "data-build-claim.json"
    competitor = b'{"competitor":true}\n'
    original_publish = data_publication.atomic_rename_no_replace_at
    calls: list[str] = []

    def compete_then_publish(
        source_fd: int,
        source: str,
        destination_fd: int,
        destination: str,
    ) -> None:
        calls.append("atomic-no-replace")
        claim_path.write_bytes(competitor)
        original_publish(source_fd, source, destination_fd, destination)

    monkeypatch.setattr(
        data_publication,
        "atomic_rename_no_replace_at",
        compete_then_publish,
    )

    with pytest.raises(ValueError, match="already claimed"):
        data_publication.claim_build(final_root, authorization)

    assert calls == ["atomic-no-replace"]
    assert claim_path.read_bytes() == competitor
    assert list(final_root.glob(".data-build-claim.*.tmp")) == []


def test_initial_claim_rejects_temporary_file_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ashare_multifactor.final_test import data_publication

    config = _config(tmp_path)
    _, authorization = _authorized_context(tmp_path, config)
    final_root = tmp_path / "claim-root"
    original_publish = data_publication.atomic_rename_no_replace_at
    forged = b'{"status":"forged"}\n'

    def substitute_then_publish(
        source_fd: int,
        source: str,
        destination_fd: int,
        destination: str,
    ) -> None:
        data_publication.os.unlink(source, dir_fd=source_fd)
        descriptor = data_publication.os.open(
            source,
            data_publication.os.O_WRONLY
            | data_publication.os.O_CREAT
            | data_publication.os.O_EXCL,
            0o600,
            dir_fd=source_fd,
        )
        with data_publication.os.fdopen(descriptor, "wb") as stream:
            stream.write(forged)
        original_publish(source_fd, source, destination_fd, destination)

    monkeypatch.setattr(
        data_publication, "atomic_rename_no_replace_at", substitute_then_publish
    )

    with pytest.raises(ValueError, match="claim.*identity|claim.*bytes"):
        data_publication.claim_build(final_root, authorization)

    assert not (final_root / "data-build-claim.json").exists()


def test_claim_transition_rejects_temporary_file_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ashare_multifactor.final_test import data_publication

    config = _config(tmp_path)
    _, authorization = _authorized_context(tmp_path, config)
    final_root = tmp_path / "claim-root"
    claim_path = data_publication.claim_build(final_root, authorization)
    original = claim_path.read_bytes()
    original_replace = data_publication.os.replace

    def substitute(source: str, destination: str, **kwargs: object) -> None:
        source_fd = int(kwargs["src_dir_fd"])
        data_publication.os.unlink(source, dir_fd=source_fd)
        descriptor = data_publication.os.open(
            source,
            data_publication.os.O_WRONLY
            | data_publication.os.O_CREAT
            | data_publication.os.O_EXCL,
            0o600,
            dir_fd=source_fd,
        )
        with data_publication.os.fdopen(descriptor, "wb") as stream:
            stream.write(b'{"status":"forged"}\n')
        original_replace(source, destination, **kwargs)

    monkeypatch.setattr(data_publication.os, "replace", substitute)

    with pytest.raises(ValueError, match="claim.*identity|claim.*bytes"):
        data_publication.update_claim(
            claim_path, authorization, status="failed", error=ValueError("stop")
        )

    assert claim_path.read_bytes() == original


def test_claim_transition_rejects_old_claim_authorization_drift(tmp_path: Path) -> None:
    from ashare_multifactor.final_test import data_publication

    config = _config(tmp_path)
    _, authorization = _authorized_context(tmp_path, config)
    final_root = tmp_path / "claim-root"
    claim_path = data_publication.claim_build(final_root, authorization)
    claim = json.loads(claim_path.read_text())
    claim["approval_id"] = "different-approval"
    claim_path.write_text(json.dumps(claim), encoding="utf-8")

    with pytest.raises(ValueError, match="claim authorization identity"):
        data_publication.update_claim(
            claim_path, authorization, status="failed", error=ValueError("stop")
        )


def test_claim_transition_resumes_exact_journal_after_process_death(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ashare_multifactor.final_test import data_publication

    config = _config(tmp_path)
    _, authorization = _authorized_context(tmp_path, config)
    final_root = tmp_path / "claim-root"
    claim_path = data_publication.claim_build(final_root, authorization)
    original_replace = data_publication.os.replace

    def process_death(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt("process died before claim install")

    monkeypatch.setattr(data_publication.os, "replace", process_death)
    with pytest.raises(KeyboardInterrupt, match="process died"):
        data_publication.update_claim(
            claim_path, authorization, status="failed", error=ValueError("stop")
        )

    assert (final_root / ".data-build-claim.json.lock").is_file()
    monkeypatch.setattr(data_publication.os, "replace", original_replace)
    data_publication.update_claim(
        claim_path, authorization, status="failed", error=ValueError("stop")
    )

    assert json.loads(claim_path.read_text())["status"] == "failed"
    assert not (final_root / ".data-build-claim.json.lock").exists()


def test_claim_transition_journal_rejects_different_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ashare_multifactor.final_test import data_publication

    config = _config(tmp_path)
    _, authorization = _authorized_context(tmp_path, config)
    final_root = tmp_path / "claim-root"
    claim_path = data_publication.claim_build(final_root, authorization)

    monkeypatch.setattr(
        data_publication.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            KeyboardInterrupt("process died before claim install")
        ),
    )
    with pytest.raises(KeyboardInterrupt):
        data_publication.update_claim(
            claim_path, authorization, status="failed", error=ValueError("first")
        )

    with pytest.raises(ValueError, match="cannot be taken over"):
        data_publication.update_claim(
            claim_path, authorization, status="failed", error=ValueError("different")
        )
