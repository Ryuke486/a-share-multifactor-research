from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

import polars as pl

from ashare_multifactor.final_test.action_source_contract import (
    load_action_source_contract,
)
from ashare_multifactor.final_test.corporate_action_candidates import (
    collect_baostock_corporate_action_candidates,
)
from ashare_multifactor.final_test.official_coverage_publisher import (
    publish_official_execution_coverages,
)
from ashare_multifactor.final_test.official_announcement_catalog import (
    build_announcement_catalog,
)
from ashare_multifactor.final_test.official_document_fetcher import (
    UrllibOfficialDocumentTransport,
    fetch_official_documents,
)
from ashare_multifactor.final_test.official_evidence_import import (
    import_compatible_official_evidence,
    load_evidence_import_source_authorization,
)
from ashare_multifactor.final_test.official_evidence_workspace import (
    EvidenceWorkspace,
)
from ashare_multifactor.final_test.official_review_submission import (
    publish_review_submission,
)
from ashare_multifactor.final_test.official_collection_monitor import (
    CollectionHeartbeatReporter,
    CollectionStatus,
    read_collection_status,
)
from ashare_multifactor.final_test.official_query_client import (
    OfficialQueryTransientError,
    RetryPolicy,
    UrllibOfficialQueryTransport,
)
from ashare_multifactor.final_test.official_query_collection_root import (
    expected_output_root,
)
from ashare_multifactor.final_test.official_query_collector import (
    QueryCollectionResult,
    collect_official_query_coverage,
)
from ashare_multifactor.final_test.official_collection_recovery import (
    CollectionProcessExit,
    ProcessRecoveryEvent,
    TransientCollectionInterruption,
    run_with_process_recovery,
    run_with_controlled_recovery,
    write_process_recovery_log,
)
from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.final_test.pipeline import resume_final_test_release
from ashare_multifactor.final_test.preparation import (
    prepare_final_test,
    verify_preparation,
)
from ashare_multifactor.final_test.registry import (
    resolve_attempt_state_readonly,
)
from ashare_multifactor.final_test.resume import load_registered_authorization


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--approval-key-file", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare or resume the authorized one-shot final test"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="authorize and prepare symbol scope")
    _add_common_arguments(prepare)
    prepare.add_argument("--opening-token", type=Path, required=True)

    resume = commands.add_parser("resume", help="verify evidence and publish")
    _add_common_arguments(resume)
    resume.add_argument("--security-event-coverage", type=Path, required=True)
    resume.add_argument("--corporate-action-coverage", type=Path, required=True)
    resume.add_argument("--run-id", required=True)

    collect = commands.add_parser(
        "collect-queries",
        help="collect public official-query coverage for a prepared attempt",
    )
    _add_common_arguments(collect)
    collect.add_argument("--output-root", type=Path, required=True)
    collect.add_argument("--max-scopes", type=_positive_int)

    supervise = commands.add_parser(
        "supervise-queries",
        help="run the query collector with at most two verified process restarts",
    )
    _add_common_arguments(supervise)
    supervise.add_argument("--output-root", type=Path, required=True)
    supervise.add_argument("--max-scopes", type=_positive_int)

    documents = commands.add_parser(
        "collect-documents",
        help="cache approved official documents for human review",
    )
    _add_common_arguments(documents)
    documents.add_argument("--output-root", type=Path, required=True)
    documents.add_argument("--max-documents", type=_positive_int)
    documents.add_argument("--retry-quarantined", action="store_true")

    candidates = commands.add_parser(
        "collect-candidates",
        help="collect resumable BaoStock candidate queries before human review",
    )
    _add_common_arguments(candidates)
    candidates.add_argument("--output-root", type=Path, required=True)
    candidates.add_argument("--max-queries", type=_positive_int)

    imported = commands.add_parser(
        "import-evidence",
        help="reuse compatible raw evidence from a predecessor attempt",
    )
    _add_common_arguments(imported)
    imported.add_argument("--output-root", type=Path, required=True)
    imported.add_argument("--source-attempt-id", required=True)

    review = commands.add_parser(
        "publish-review",
        help="validate and freeze field-level human review decisions",
    )
    _add_common_arguments(review)
    review.add_argument("--output-root", type=Path, required=True)
    review.add_argument("--review-queue", type=Path, required=True)
    review.add_argument("--candidate-manifest", type=Path, required=True)
    review.add_argument("--announcement-decisions", type=Path, required=True)
    review.add_argument("--corporate-dispositions", type=Path, required=True)
    review.add_argument("--corporate-facts", type=Path, required=True)
    review.add_argument("--security-facts", type=Path, required=True)
    review.add_argument("--reviewer-id", required=True)
    review.add_argument("--reviewed-at", required=True)

    coverages = commands.add_parser(
        "publish-coverages",
        help="atomically publish corporate-action and security-event coverages",
    )
    _add_common_arguments(coverages)
    coverages.add_argument("--output-root", type=Path, required=True)
    coverages.add_argument("--review-queue", type=Path, required=True)
    coverages.add_argument("--candidate-manifest", type=Path, required=True)
    coverages.add_argument("--submission-manifest", type=Path, required=True)
    coverages.add_argument("--official-query-index", type=Path, required=True)

    monitor = commands.add_parser(
        "monitor-collection",
        help="read one collector heartbeat without changing attempt state",
    )
    monitor.add_argument("--data-root", type=Path, required=True)
    monitor.add_argument("--attempt-id", required=True)
    monitor.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    if args.command == "monitor-collection":
        print(_format_collection_status(_monitor_collection(args)))
        return

    approval_key = args.approval_key_file.read_bytes()
    if args.command == "prepare":
        result = prepare_final_test(
            code_root=args.root,
            data_root=args.data_root,
            opening_token_path=args.opening_token,
            approval_key=approval_key,
            attempt_id=args.attempt_id,
        )
        print(result.root)
        return

    if args.command == "supervise-queries":
        print(_supervise_queries(args, approval_key=approval_key))
        return

    if args.command == "collect-queries":
        authorization = load_registered_authorization(
            code_root=args.root,
            data_root=args.data_root,
            attempt_id=args.attempt_id,
            approval_key=approval_key,
        )
        preparation = verify_preparation(
            args.data_root / "processed/final_test",
            attempt_id=args.attempt_id,
            authorization=authorization,
        )
        reporter = CollectionHeartbeatReporter(
            args.output_root / "collection_heartbeat.json",
            identity=_heartbeat_identity(authorization, preparation.manifest_sha256),
            total_securities=preparation.symbol_count,
            now=lambda: datetime.now(timezone.utc),
        )
        contract = load_action_source_contract(
            args.root / "configs/final_execution_sources.yaml"
        )
        transport = UrllibOfficialQueryTransport()
        policy = RetryPolicy()

        def collect() -> QueryCollectionResult:
            try:
                return collect_official_query_coverage(
                    preparation=preparation,
                    authorization=authorization,
                    contract=contract,
                    output_root=args.output_root,
                    transport=transport,
                    policy=policy,
                    max_scopes=args.max_scopes,
                    progress_observer=reporter,
                )
            except OfficialQueryTransientError as error:
                raise TransientCollectionInterruption(str(error)) from error

        def verify_identity() -> None:
            recovered_authorization = load_registered_authorization(
                code_root=args.root,
                data_root=args.data_root,
                attempt_id=args.attempt_id,
                approval_key=approval_key,
            )
            recovered_preparation = verify_preparation(
                args.data_root / "processed/final_test",
                attempt_id=args.attempt_id,
                authorization=recovered_authorization,
            )
            if (
                recovered_authorization != authorization
                or recovered_preparation != preparation
            ):
                raise ValueError("official collection recovery identity differs")

        recovery = run_with_controlled_recovery(
            collect,
            verify_identity=verify_identity,
        )
        result = recovery.value
        if not isinstance(result, QueryCollectionResult):
            raise TypeError("official collection recovery result is invalid")
        reporter.finish(completed=result.completed_scopes)
        print(result.index_path or result.root)
        return

    if args.command == "collect-documents":
        authorization = load_registered_authorization(
            code_root=args.root,
            data_root=args.data_root,
            attempt_id=args.attempt_id,
            approval_key=approval_key,
        )
        preparation = verify_preparation(
            args.data_root / "processed/final_test",
            attempt_id=args.attempt_id,
            authorization=authorization,
        )
        contract = load_action_source_contract(
            args.root / "configs/final_execution_sources.yaml"
        )
        catalog_path = build_announcement_catalog(
            args.output_root / "official_query_coverage/official_query_coverage.json",
            preparation=preparation,
            authorization=authorization,
            contract=contract,
            destination=args.output_root,
        )
        workspace = fetch_official_documents(
            catalog_path,
            destination=args.output_root,
            transport=UrllibOfficialDocumentTransport(),
            max_documents=args.max_documents,
            retry_quarantined=args.retry_quarantined,
        )
        print(workspace.review_queue_path)
        return

    if args.command == "collect-candidates":
        authorization, preparation, contract = _load_prepared_inputs(
            args,
            approval_key=approval_key,
        )
        result = collect_baostock_corporate_action_candidates(
            preparation=preparation,
            authorization=authorization,
            contract=contract,
            output_root=args.output_root,
            max_queries=args.max_queries,
        )
        print(result.manifest_path or result.root)
        return

    if args.command == "import-evidence":
        authorization, preparation, contract = _load_prepared_inputs(
            args,
            approval_key=approval_key,
        )
        source_authorization = load_evidence_import_source_authorization(
            code_root=args.root,
            data_root=args.data_root,
            attempt_id=args.source_attempt_id,
        )
        source_preparation = verify_preparation(
            args.data_root / "processed/final_test",
            attempt_id=args.source_attempt_id,
            authorization=source_authorization,
        )
        source_root = expected_output_root(
            args.data_root / "processed/final_test",
            args.source_attempt_id,
        )
        result = import_compatible_official_evidence(
            source_preparation=source_preparation,
            source_authorization=source_authorization,
            destination_preparation=preparation,
            destination_authorization=authorization,
            contract=contract,
            source_root=source_root,
            destination_root=args.output_root,
        )
        print(result.workspace.review_queue_path)
        print(f"missing_documents={len(result.missing_urls)}")
        return

    if args.command == "publish-review":
        _authorization, _preparation, _contract = _load_prepared_inputs(
            args,
            approval_key=approval_key,
        )
        _assert_attempt_output_root(args)
        workspace = _workspace_from_queue(args.output_root, args.review_queue)
        result = publish_review_submission(
            workspace=workspace,
            candidate_manifest_path=args.candidate_manifest,
            announcement_decisions=pl.read_parquet(args.announcement_decisions),
            corporate_action_dispositions=pl.read_parquet(
                args.corporate_dispositions
            ),
            corporate_action_facts=pl.read_parquet(args.corporate_facts),
            security_event_facts=pl.read_parquet(args.security_facts),
            reviewer_id=args.reviewer_id,
            reviewed_at=args.reviewed_at,
        )
        print(result.manifest_path)
        return

    if args.command == "publish-coverages":
        authorization, preparation, contract = _load_prepared_inputs(
            args,
            approval_key=approval_key,
        )
        workspace = _workspace_from_queue(args.output_root, args.review_queue)
        result = publish_official_execution_coverages(
            preparation=preparation,
            authorization=authorization,
            contract=contract,
            workspace=workspace,
            candidate_manifest_path=args.candidate_manifest,
            submission_manifest_path=args.submission_manifest,
            official_query_index_path=args.official_query_index,
        )
        print(result.root)
        return

    result = resume_final_test_release(
        code_root=args.root,
        data_root=args.data_root,
        approval_key=approval_key,
        attempt_id=args.attempt_id,
        security_event_coverage_path=args.security_event_coverage,
        corporate_action_coverage_root=args.corporate_action_coverage,
        run_id=args.run_id,
    )
    print(result.release.root if result.release is not None else result.attempt_root)


def _load_prepared_inputs(
    args: argparse.Namespace,
    *,
    approval_key: bytes,
) -> tuple[FinalTestAuthorization, object, object]:
    authorization = load_registered_authorization(
        code_root=args.root,
        data_root=args.data_root,
        attempt_id=args.attempt_id,
        approval_key=approval_key,
    )
    preparation = verify_preparation(
        args.data_root / "processed/final_test",
        attempt_id=args.attempt_id,
        authorization=authorization,
    )
    contract = load_action_source_contract(
        args.root / "configs/final_execution_sources.yaml"
    )
    return authorization, preparation, contract


def _workspace_from_queue(
    output_root: Path,
    review_queue: Path,
) -> EvidenceWorkspace:
    workspace_root = output_root / "official_document_workspace"
    expected_parent = workspace_root / "review_sessions"
    if (
        review_queue.name != "review_queue.parquet"
        or review_queue.parent.parent.absolute() != expected_parent.absolute()
    ):
        raise ValueError("review queue differs from the official evidence workspace")
    return EvidenceWorkspace(
        root=workspace_root,
        catalog_path=output_root / "official_announcement_catalog/catalog.parquet",
        routing_path=output_root / "official_announcement_routing/routing.parquet",
        review_queue_path=review_queue,
        ready=False,
    )


def _assert_attempt_output_root(args: argparse.Namespace) -> None:
    expected = expected_output_root(
        args.data_root / "processed/final_test",
        args.attempt_id,
    )
    if args.output_root.absolute() != expected.absolute():
        raise ValueError("official evidence output root differs from attempt")


def _monitor_collection(args: argparse.Namespace) -> CollectionStatus:
    final_root = args.data_root / "processed/final_test"
    expected_root = expected_output_root(final_root, args.attempt_id)
    if args.output_root.absolute() != expected_root.absolute():
        raise ValueError("official collection monitor output root differs from attempt")
    state = resolve_attempt_state_readonly(
        final_root / "attempts",
        args.attempt_id,
    )
    prepare_sha256 = state.get("identities", {}).get("prepare_manifest_sha256")
    identity = {
        "attempt_id": args.attempt_id,
        "git_commit": state.get("git_commit"),
        "git_tree": state.get("git_tree"),
        "sealed_protocol_sha256": state.get("sealed_protocol_sha256"),
        "prepare_manifest_sha256": prepare_sha256,
    }
    return read_collection_status(
        args.output_root / "collection_heartbeat.json",
        expected_identity=identity,
        now=datetime.now(timezone.utc),
    )


def _supervise_queries(
    args: argparse.Namespace,
    *,
    approval_key: bytes,
) -> Path:
    authorization = load_registered_authorization(
        code_root=args.root,
        data_root=args.data_root,
        attempt_id=args.attempt_id,
        approval_key=approval_key,
    )
    preparation = verify_preparation(
        args.data_root / "processed/final_test",
        attempt_id=args.attempt_id,
        authorization=authorization,
    )
    expected_root = expected_output_root(
        args.data_root / "processed/final_test",
        args.attempt_id,
    )
    if args.output_root.absolute() != expected_root.absolute():
        raise ValueError("official collection supervisor output root differs from attempt")
    identity = _heartbeat_identity(authorization, preparation.manifest_sha256)
    command = _collector_command(args)
    observed: list[ProcessRecoveryEvent] = []

    def verify_identity() -> None:
        recovered_authorization = load_registered_authorization(
            code_root=args.root,
            data_root=args.data_root,
            attempt_id=args.attempt_id,
            approval_key=approval_key,
        )
        recovered_preparation = verify_preparation(
            args.data_root / "processed/final_test",
            attempt_id=args.attempt_id,
            authorization=recovered_authorization,
        )
        if (
            recovered_authorization != authorization
            or recovered_preparation != preparation
        ):
            raise ValueError("official collection recovery identity differs")

    def observe(event: ProcessRecoveryEvent) -> None:
        observed.append(event)
        status = "running" if event.kind == "launch" else "interrupted"
        if event.kind == "exit" and event.returncode == 0:
            status = "complete"
        write_process_recovery_log(
            args.data_root,
            attempt_id=args.attempt_id,
            identity=identity,
            status=status,
            restart_count=_restart_count(observed),
            events=observed,
        )

    try:
        result = run_with_process_recovery(
            lambda: _start_collector_process(command, cwd=args.root),
            verify_identity=verify_identity,
            event_observer=observe,
        )
    except CollectionProcessExit as error:
        write_process_recovery_log(
            args.data_root,
            attempt_id=args.attempt_id,
            identity=identity,
            status="failed",
            restart_count=error.restart_count,
            events=observed,
        )
        raise
    return write_process_recovery_log(
        args.data_root,
        attempt_id=args.attempt_id,
        identity=identity,
        status="complete",
        restart_count=result.restart_count,
        events=observed,
    )


def _collector_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "ashare_multifactor.cli.final_test",
        "collect-queries",
        "--root",
        str(args.root),
        "--data-root",
        str(args.data_root),
        "--approval-key-file",
        str(args.approval_key_file),
        "--attempt-id",
        args.attempt_id,
        "--output-root",
        str(args.output_root),
    ]
    if args.max_scopes is not None:
        command.extend(["--max-scopes", str(args.max_scopes)])
    return command


def _start_collector_process(
    command: list[str],
    *,
    cwd: Path,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(command, cwd=cwd)


def _restart_count(events: Sequence[ProcessRecoveryEvent]) -> int:
    return sum(
        event.kind == "launch" and event.predecessor_pid is not None
        for event in events
    )


def _heartbeat_identity(
    authorization: FinalTestAuthorization,
    prepare_manifest_sha256: str,
) -> dict[str, str]:
    return {
        "attempt_id": authorization.attempt_id,
        "git_commit": authorization.git_commit,
        "git_tree": authorization.git_tree,
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "prepare_manifest_sha256": prepare_manifest_sha256,
    }


def _format_collection_status(status: CollectionStatus) -> str:
    start, end = status.current_period
    page, pages = status.current_page
    return (
        f"{status.level} | {status.phase} "
        f"{status.completed_securities}/{status.total_securities} | "
        f"current {status.current_symbol} {start}..{end} page {page}/{pages} | "
        f"requests {status.request_count} | splits {status.split_count} | "
        f"ETA {status.eta_seconds}s | {status.reason}"
    )


if __name__ == "__main__":
    main()
