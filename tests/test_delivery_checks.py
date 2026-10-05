import json
import subprocess
from pathlib import Path

import pytest

from ashare_multifactor.audit.delivery import (
    KeyResultSpec,
    ResultSelect,
    SourceSpec,
    build_key_results,
    check_delivery_manifest,
    check_documented_results,
    check_markdown_links,
    number_renders_value,
    run_delivery_checks,
    verify_key_result_sources,
    write_key_results,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True)


def _init_repository(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "checks@example.invalid")
    _git(root, "config", "user.name", "delivery checks")


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


# --- Markdown links -------------------------------------------------------


def test_markdown_links_accept_relative_external_and_anchor_targets(tmp_path: Path) -> None:
    (tmp_path / "target.md").write_text("# target\n", encoding="utf-8")
    (tmp_path / "index.md").write_text(
        "[local](target.md)\n[anchor](#section)\n[web](https://example.invalid/x)\n",
        encoding="utf-8",
    )

    outcome = check_markdown_links(tmp_path, ("index.md",))

    assert outcome.ok, outcome.issues
    assert outcome.notes == ("1 local links checked in 1 documents",)


def test_markdown_links_report_a_missing_target(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text("[gone](missing.md)\n", encoding="utf-8")

    outcome = check_markdown_links(tmp_path, ("index.md",))

    assert not outcome.ok
    assert "does not exist" in outcome.issues[0].detail


def test_markdown_links_reject_a_git_ignored_target(tmp_path: Path) -> None:
    _init_repository(tmp_path)
    (tmp_path / ".gitignore").write_text("processed/\n", encoding="utf-8")
    (tmp_path / "index.md").write_text("[local](processed/nav.parquet)\n", encoding="utf-8")
    _commit(tmp_path, "docs")
    (tmp_path / "processed").mkdir()
    (tmp_path / "processed" / "nav.parquet").write_bytes(b"local only")

    outcome = check_markdown_links(tmp_path, ("index.md",))

    assert not outcome.ok
    assert "not deliverable" in outcome.issues[0].detail


# --- Manifest hashes ------------------------------------------------------


def _manifest(root: Path, files: dict[str, str], **extra: object) -> None:
    payload = {
        "code_baseline": {"commit": "", "tree": ""},
        "delivery_files": [
            {"path": path, "sha256": digest} for path, digest in sorted(files.items())
        ],
        "final_test": {"current_pointer_exists": False},
    }
    payload.update(extra)
    (root / "releases").mkdir(exist_ok=True)
    (root / "releases" / "delivery.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def _sha(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def test_delivery_manifest_detects_a_changed_digest(tmp_path: Path) -> None:
    _init_repository(tmp_path)
    (tmp_path / "README.md").write_text("delivered\n", encoding="utf-8")
    _commit(tmp_path, "content")
    _manifest(tmp_path, {"README.md": "0" * 64})
    _commit(tmp_path, "manifest")

    outcome = check_delivery_manifest(tmp_path, Path("releases/delivery.json"))

    assert not outcome.ok
    assert "do not match the recorded hash" in outcome.issues[0].detail


def test_delivery_manifest_ignores_later_worktree_work(tmp_path: Path) -> None:
    _init_repository(tmp_path)
    (tmp_path / "README.md").write_text("delivered\n", encoding="utf-8")
    _commit(tmp_path, "content")
    _manifest(tmp_path, {"README.md": _sha(b"delivered\n")})
    _commit(tmp_path, "manifest")
    (tmp_path / "README.md").write_text("later authorized work\n", encoding="utf-8")

    outcome = check_delivery_manifest(tmp_path, Path("releases/delivery.json"))

    assert outcome.ok, outcome.issues
    assert any("differs from the published delivery" in note for note in outcome.notes)


def test_delivery_manifest_detects_a_broken_final_test_seal(tmp_path: Path) -> None:
    _init_repository(tmp_path)
    _manifest(tmp_path, {}, final_test={"current_pointer_exists": True})
    _commit(tmp_path, "manifest")

    outcome = check_delivery_manifest(tmp_path, Path("releases/delivery.json"))

    assert not outcome.ok
    assert "final-test seal state" in outcome.issues[0].detail


# --- Number rendering -----------------------------------------------------


@pytest.mark.parametrize(
    ("value", "literal"),
    [
        (0.17603097829253445, "17.6031"),
        (0.17603097829253445, "0.17603097829253445"),
        (-0.6696382491083639, "-66.9638"),
        (105271.0, "105,271"),
        (2.384185791015625e-07, "0.00000024"),
        (16.109088864309605, "16.11"),
    ],
)
def test_number_renders_value_accepts_each_documented_precision(
    value: float, literal: str
) -> None:
    assert number_renders_value(value, literal)


def test_number_renders_value_rejects_a_changed_digit() -> None:
    assert not number_renders_value(0.17603097829253445, "17.9031")
    assert not number_renders_value(0.17603097829253445, "0.1761")


# --- Documented numbers ---------------------------------------------------


def _number_fixture(tmp_path: Path, document_text: str = "年化收益 17.6031%。\n") -> Path:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    (artifacts / "summary.json").write_text(
        json.dumps({"annual_return": 0.17603097829253445}), encoding="utf-8"
    )
    (tmp_path / "report.md").write_text(document_text, encoding="utf-8")
    specs = (
        KeyResultSpec(
            "fixture.annual_return",
            "fixture",
            ResultSelect("summary.json", field="annual_return"),
            required_documents=("report.md",),
        ),
    )
    sources = (SourceSpec("fixture", "file", "artifacts"),)
    return write_key_results(
        tmp_path,
        Path("docs/results/key.json"),
        specs=specs,
        sources=sources,
        documents=("report.md",),
    )


def test_recorded_numbers_accept_the_unchanged_document(tmp_path: Path) -> None:
    _number_fixture(tmp_path)

    outcome = check_documented_results(tmp_path, Path("docs/results/key.json"))

    assert outcome.ok, outcome.issues


def test_recorded_numbers_detect_a_changed_document_number(tmp_path: Path) -> None:
    _number_fixture(tmp_path)
    (tmp_path / "report.md").write_text("年化收益 17.9031%。\n", encoding="utf-8")

    outcome = check_documented_results(tmp_path, Path("docs/results/key.json"))

    assert not outcome.ok
    assert "no longer stated" in outcome.issues[0].detail


def test_recorded_numbers_detect_a_literal_that_no_longer_renders_the_value(
    tmp_path: Path,
) -> None:
    path = _number_fixture(tmp_path)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["results"][0]["value"] = 0.5
    path.write_text(json.dumps(record), encoding="utf-8")

    outcome = check_documented_results(tmp_path, Path("docs/results/key.json"))

    assert not outcome.ok
    assert "no longer renders" in outcome.issues[0].detail


def test_recording_rejects_a_required_document_without_the_number(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "summary.json").write_text(json.dumps({"return": 0.5}), encoding="utf-8")
    (tmp_path / "report.md").write_text("没有数字。\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no citation recorded"):
        build_key_results(
            tmp_path,
            specs=(
                KeyResultSpec(
                    "fixture.return",
                    "fixture",
                    ResultSelect("summary.json", field="return"),
                    required_documents=("report.md",),
                ),
            ),
            sources=(SourceSpec("fixture", "file", "artifacts"),),
            documents=("report.md",),
        )


def test_recording_resolves_a_filtered_ratio_of_column_sums(tmp_path: Path) -> None:
    import polars as pl

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    pl.DataFrame(
        {
            "side": ["buy", "buy", "sell"],
            "quantity": [1000, 3000, 500],
            "remaining_quantity": [100, 0, 500],
        }
    ).write_parquet(artifacts / "orders.parquet")
    (tmp_path / "report.md").write_text("买单未成交股数占比 2.50%。\n", encoding="utf-8")

    record = build_key_results(
        tmp_path,
        specs=(
            KeyResultSpec(
                "fixture.buy_unfilled_share",
                "fixture",
                ResultSelect(
                    "orders.parquet",
                    kind="parquet",
                    field="remaining_quantity",
                    denominator="quantity",
                    where=(("side", "buy"),),
                    stat="sum_ratio",
                ),
                required_documents=("report.md",),
            ),
        ),
        sources=(SourceSpec("fixture", "file", "artifacts"),),
        documents=("report.md",),
    )

    assert record["results"][0]["value"] == pytest.approx(0.025)
    assert record["results"][0]["citations"][0]["literal"] == "2.50"


def test_source_verification_detects_a_changed_artifact(tmp_path: Path) -> None:
    _number_fixture(tmp_path)
    (tmp_path / "artifacts" / "summary.json").write_text(
        json.dumps({"annual_return": 0.9}), encoding="utf-8"
    )

    outcome = verify_key_result_sources(tmp_path, Path("docs/results/key.json"))

    assert not outcome.ok
    assert "artifact bytes changed" in outcome.issues[0].detail


def test_source_verification_is_skipped_without_local_releases(tmp_path: Path) -> None:
    path = _number_fixture(tmp_path)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["sources"] = [
        {
            "name": "stage7_validation_evaluation",
            "kind": "release",
            "root": "processed/validation_evaluation",
            "run_id": "missing",
            "manifest_sha256": "0" * 64,
        }
    ]
    path.write_text(json.dumps(record), encoding="utf-8")

    outcome = verify_key_result_sources(tmp_path, Path("docs/results/key.json"))

    assert outcome.ok
    assert outcome.skipped is not None
    assert "local sources absent" in outcome.skipped


# --- Repository-level guard ----------------------------------------------


def test_repository_delivery_checks_pass() -> None:
    report = run_delivery_checks(REPOSITORY_ROOT)

    assert report.ok, report.render()
