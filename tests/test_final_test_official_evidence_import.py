from __future__ import annotations

from dataclasses import replace
import json
import subprocess
from types import SimpleNamespace

import polars as pl
import pytest

from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.final_test import preparation as preparation_module
from ashare_multifactor.final_test import official_evidence_import as import_module
from ashare_multifactor.final_test.final_root_binding import FinalRootBinding
from ashare_multifactor.final_test.official_announcement_catalog import (
    build_announcement_catalog,
)
from ashare_multifactor.final_test.official_document_fetcher import (
    DocumentFetchPolicy,
    fetch_official_documents,
)
from ashare_multifactor.final_test.official_evidence_import import (
    OfficialEvidenceImportInputs,
    complete_compatible_official_evidence_import,
    import_compatible_official_evidence,
    import_compatible_official_evidence_inputs,
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

    imported_inputs = import_compatible_official_evidence_inputs(
        source_preparation=source.preparation,
        source_authorization=source.authorization,
        destination_preparation=destination_preparation,
        destination_authorization=destination_authorization,
        contract=source.contract,
        source_root=source.output_root,
        destination_root=destination_root,
    )
    assert imported_inputs.query_index_path.is_file()
    assert imported_inputs.identity_index_path.is_file()
    assert not (destination_root / "official_announcement_catalog").exists()
    assert not (destination_root / "official_announcement_routing").exists()
    assert not (destination_root / "official_document_workspace").exists()

    imported = complete_compatible_official_evidence_import(imported_inputs)

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


def test_phase_two_rejects_replaced_source_root_before_pdf_access(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    source = _complete_query_coverage(prepared_attempt)
    destination_authorization, destination_preparation = _destination_attempt(
        prepared_attempt,
        source.authorization,
    )
    _allow_fixture_destination_preparation(
        monkeypatch,
        destination_attempt_id=destination_authorization.attempt_id,
    )
    destination_root = (
        prepared_attempt.data_root
        / "processed/final_test_evidence"
        / destination_authorization.attempt_id
    )
    inputs = import_compatible_official_evidence_inputs(
        source_preparation=source.preparation,
        source_authorization=source.authorization,
        destination_preparation=destination_preparation,
        destination_authorization=destination_authorization,
        contract=source.contract,
        source_root=source.output_root,
        destination_root=destination_root,
    )

    def forbidden_pdf_access(*args: object, **kwargs: object) -> None:
        raise AssertionError("phase-two validation must precede PDF access")

    monkeypatch.setattr(import_module, "_load_reusable_document", forbidden_pdf_access)
    with pytest.raises(ValueError, match="source root differs"):
        complete_compatible_official_evidence_import(
            replace(inputs, source_root=tmp_path / "foreign-source")
        )

    assert not (destination_root / "official_announcement_catalog").exists()
    assert not (destination_root / "official_announcement_routing").exists()
    assert not (destination_root / "official_document_workspace").exists()


@pytest.mark.parametrize(
    "drift",
    (
        "destination_root",
        "same_attempt_manual_inputs",
        "source_preparation_attempt_and_root",
        "source_authorization",
        "query_index",
        "identity_index",
        "symbol_scope",
        "contract",
    ),
)
def test_phase_two_revalidates_complete_phase_one_provenance_before_publication(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    drift: str,
) -> None:
    source, destination_root, inputs = _phase_one_inputs(
        prepared_attempt,
        monkeypatch,
    )
    if drift == "destination_root":
        candidate = replace(inputs, destination_root=tmp_path / "foreign-destination")
    elif drift == "same_attempt_manual_inputs":
        candidate = OfficialEvidenceImportInputs(
            source_preparation=source.preparation,
            source_authorization=source.authorization,
            destination_preparation=source.preparation,
            destination_authorization=source.authorization,
            contract=source.contract,
            source_root=source.output_root,
            destination_root=source.output_root,
            query_index_path=(
                source.output_root
                / "official_query_coverage/official_query_coverage.json"
            ),
            identity_index_path=(
                source.output_root
                / "official_security_identities/official_security_identities.json"
            ),
        )
    elif drift == "source_preparation_attempt_and_root":
        candidate = replace(
            inputs,
            source_preparation=replace(
                inputs.source_preparation,
                attempt_id=inputs.destination_preparation.attempt_id,
                root=inputs.destination_preparation.root,
                manifest_path=inputs.destination_preparation.manifest_path,
                symbol_scope_path=inputs.destination_preparation.symbol_scope_path,
            ),
        )
    elif drift == "source_authorization":
        candidate = replace(
            inputs,
            source_authorization=replace(
                inputs.source_authorization,
                approval_id="drifted-approval",
            ),
        )
    elif drift == "query_index":
        candidate = OfficialEvidenceImportInputs(
            **{
                **inputs.__dict__,
                "query_index_path": (
                    source.output_root
                    / "official_query_coverage/official_query_coverage.json"
                ),
            }
        )
    elif drift == "identity_index":
        candidate = replace(
            inputs,
            identity_index_path=(
                source.output_root
                / "official_security_identities/official_security_identities.json"
            ),
        )
    elif drift == "symbol_scope":
        candidate = replace(
            inputs,
            source_preparation=replace(
                inputs.source_preparation,
                symbol_scope_path=inputs.destination_preparation.symbol_scope_path,
            ),
        )
    elif drift == "contract":
        candidate = replace(
            inputs,
            contract=replace(inputs.contract, supported_markets=("sh",)),
        )
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(drift)

    def forbidden_publication(*args: object, **kwargs: object) -> None:
        raise AssertionError("phase-two validation must precede publication")

    monkeypatch.setattr(
        import_module,
        "build_announcement_catalog",
        forbidden_publication,
    )
    monkeypatch.setattr(
        import_module,
        "_load_reusable_document",
        forbidden_publication,
    )
    with pytest.raises(ValueError):
        complete_compatible_official_evidence_import(candidate)

    assert not (destination_root / "official_announcement_catalog").exists()
    assert not (destination_root / "official_announcement_routing").exists()
    assert not (destination_root / "official_document_workspace").exists()


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


def _allow_fixture_destination_preparation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    destination_attempt_id: str,
) -> None:
    original_verify_binding = preparation_module.verify_preparation_data_binding

    def verify_fixture_binding(*args: object, **kwargs: object) -> None:
        authorization = args[1]
        if authorization.attempt_id == destination_attempt_id:
            return
        original_verify_binding(*args, **kwargs)

    monkeypatch.setattr(
        preparation_module,
        "verify_preparation_data_binding",
        verify_fixture_binding,
    )


def _phase_one_inputs(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
):
    source = _complete_query_coverage(prepared_attempt)
    destination_authorization, destination_preparation = _destination_attempt(
        prepared_attempt,
        source.authorization,
    )
    _allow_fixture_destination_preparation(
        monkeypatch,
        destination_attempt_id=destination_authorization.attempt_id,
    )
    destination_root = (
        prepared_attempt.data_root
        / "processed/final_test_evidence"
        / destination_authorization.attempt_id
    )
    inputs = import_compatible_official_evidence_inputs(
        source_preparation=source.preparation,
        source_authorization=source.authorization,
        destination_preparation=destination_preparation,
        destination_authorization=destination_authorization,
        contract=source.contract,
        source_root=source.output_root,
        destination_root=destination_root,
    )
    return source, destination_root, inputs
