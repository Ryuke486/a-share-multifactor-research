from __future__ import annotations

from dataclasses import replace
import json
import subprocess
from types import SimpleNamespace

import polars as pl
import pytest

from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.final_test import preparation as preparation_module
from ashare_multifactor.final_test.final_root_binding import FinalRootBinding
from ashare_multifactor.final_test.official_announcement_catalog import (
    build_announcement_catalog,
)
from ashare_multifactor.final_test.official_document_fetcher import (
    DocumentFetchPolicy,
    fetch_official_documents,
)
from ashare_multifactor.final_test.official_evidence_import import (
    import_compatible_official_evidence,
    load_evidence_import_source_authorization,
)
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes
from ashare_multifactor.final_test.preparation import _publish_preparation
from ashare_multifactor.final_test.registry import (
    append_attempt_state,
    register_attempt,
)
from test_final_test_official_evidence_workspace import (
    ScriptedDocumentTransport,
    _complete_query_coverage,
    _pdf_bytes,
)
from test_final_test_resume import PreparedAttempt


pytest_plugins = ("test_final_test_resume",)


def test_source_attempt_can_be_verified_after_code_head_moves(
    prepared_attempt: PreparedAttempt,
) -> None:
    successor = prepared_attempt.code_root / "evidence-successor.txt"
    successor.write_text("successor\n", encoding="utf-8")
    subprocess.run(
        ("git", "add", successor.name),
        cwd=prepared_attempt.code_root,
        check=True,
    )
    subprocess.run(
        ("git", "commit", "-m", "evidence successor"),
        cwd=prepared_attempt.code_root,
        check=True,
        capture_output=True,
    )

    authorization = load_evidence_import_source_authorization(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        attempt_id=prepared_attempt.attempt_id,
    )

    assert authorization.attempt_id == prepared_attempt.attempt_id
    assert authorization.git_commit != subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=prepared_attempt.code_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_evidence_import_reuses_complete_queries_and_only_valid_legacy_documents(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _complete_query_coverage(prepared_attempt)
    catalog = build_announcement_catalog(
        source.index_path,
        preparation=source.preparation,
        authorization=source.authorization,
        contract=source.contract,
        destination=source.output_root,
    )
    fetch_official_documents(
        catalog,
        destination=source.output_root,
        transport=ScriptedDocumentTransport(
            [_pdf_bytes(), b"<html>official endpoint error</html>"]
        ),
        policy=DocumentFetchPolicy(
            attempts=1,
            timeout_seconds=0.1,
            minimum_interval_seconds=0,
        ),
    )
    valid_manifest = next(
        (
            source.output_root
            / "official_document_workspace/documents"
        ).rglob("document_manifest.json")
    )
    legacy = json.loads(valid_manifest.read_text(encoding="utf-8"))
    legacy["schema_version"] = "1"
    legacy.pop("media_type")
    legacy.pop("page_count")
    valid_manifest.write_bytes(canonical_json_bytes(legacy))

    destination_authorization, destination_preparation = _destination_attempt(
        prepared_attempt,
        source.authorization,
    )
    original_verify_binding = preparation_module.verify_preparation_data_binding

    def verify_fixture_binding(*args: object, **kwargs: object) -> None:
        authorization = args[1]
        if authorization.attempt_id == destination_authorization.attempt_id:
            return
        original_verify_binding(*args, **kwargs)

    monkeypatch.setattr(
        preparation_module,
        "verify_preparation_data_binding",
        verify_fixture_binding,
    )
    destination_root = (
        prepared_attempt.data_root
        / "processed/final_test_evidence"
        / destination_authorization.attempt_id
    )

    imported = import_compatible_official_evidence(
        source_preparation=source.preparation,
        source_authorization=source.authorization,
        destination_preparation=destination_preparation,
        destination_authorization=destination_authorization,
        contract=source.contract,
        source_root=source.output_root,
        destination_root=destination_root,
    )

    assert imported.query_index_path.is_file()
    assert imported.identity_index_path.is_file()
    assert imported.reused_document_count == 1
    assert len(imported.missing_urls) == 1
    assert [item.reason for item in imported.rejected_documents] == ["not_pdf"]
    destination_queue = pl.read_parquet(imported.workspace.review_queue_path)
    assert set(destination_queue.get_column("document_status")) == {
        "cached",
        "not_fetched",
    }
    destination_manifests = list(
        (
            destination_root
            / "official_document_workspace/documents"
        ).rglob("document_manifest.json")
    )
    assert len(destination_manifests) == 1
    assert json.loads(destination_manifests[0].read_text())["schema_version"] == "2"
    source_request = next(
        (source.output_root / "official_query_coverage").rglob("request.json")
    )
    destination_request = (
        destination_root
        / "official_query_coverage"
        / source_request.relative_to(
            source.output_root / "official_query_coverage"
        )
    )
    assert destination_request.read_bytes() == source_request.read_bytes()
    identity_index = json.loads(imported.identity_index_path.read_text())
    assert identity_index["attempt_id"] == destination_authorization.attempt_id

    recovered = import_compatible_official_evidence(
        source_preparation=source.preparation,
        source_authorization=source.authorization,
        destination_preparation=destination_preparation,
        destination_authorization=destination_authorization,
        contract=source.contract,
        source_root=source.output_root,
        destination_root=destination_root,
    )
    assert recovered.query_index_path == imported.query_index_path
    assert recovered.missing_urls == imported.missing_urls


def _destination_attempt(
    attempt: PreparedAttempt,
    source_authorization: object,
):
    final_root = attempt.data_root / "processed/final_test"
    destination_authorization = replace(
        source_authorization,
        attempt_id="stage9-import-destination",
        approval_id="import-approval",
        registered_at="2026-07-27T12:00:00+08:00",
    )
    panel_root = final_root / "daily_panel"
    data_manifest = panel_root / "data_manifest.json"
    resolution = SimpleNamespace(
        root=panel_root,
        claim_status="published",
        requires_recovery=False,
        data_manifest_sha256=sha256_file(data_manifest),
    )
    symbols = (
        pl.read_parquet(attempt.symbol_scope)
        .get_column("symbol")
        .cast(pl.String)
        .to_list()
    )
    with FinalRootBinding.open(final_root) as binding:
        preparation = _publish_preparation(
            final_root,
            authorization=destination_authorization,
            resolution=resolution,
            symbols=symbols,
            data_binding={
                "mode": "verified_reuse",
                "relative_path": (
                    f"data-reuse/{destination_authorization.attempt_id}.json"
                ),
                "sha256": "1" * 64,
                "size_bytes": 1,
            },
            root_binding=binding,
        )
    registry = final_root / "attempts"
    register_attempt(
        registry,
        attempt_id=destination_authorization.attempt_id,
        git_commit=destination_authorization.git_commit,
        git_tree=destination_authorization.git_tree,
        token_sha256="0" * 64,
        sealed_protocol_sha256=destination_authorization.sealed_protocol_sha256,
        robustness_release=destination_authorization.robustness_release,
        robustness_manifest_sha256=(
            destination_authorization.robustness_manifest_sha256
        ),
        robustness_lineage_sha256=(
            destination_authorization.robustness_lineage_sha256
        ),
        approval_id=destination_authorization.approval_id,
    )
    append_attempt_state(
        registry,
        attempt_id=destination_authorization.attempt_id,
        state="preparing",
    )
    append_attempt_state(
        registry,
        attempt_id=destination_authorization.attempt_id,
        state="awaiting_official_evidence",
        identities={"prepare_manifest_sha256": preparation.manifest_sha256},
    )
    return destination_authorization, preparation
