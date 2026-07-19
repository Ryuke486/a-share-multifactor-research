from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from ashare_multifactor.final_test.action_source_contract import (
    load_action_source_contract,
)
from ashare_multifactor.final_test.official_announcement_catalog import (
    build_announcement_catalog,
)
from ashare_multifactor.final_test.official_document_fetcher import (
    UrllibOfficialDocumentTransport,
    fetch_official_documents,
)
from ashare_multifactor.final_test.official_query_client import (
    RetryPolicy,
    UrllibOfficialQueryTransport,
)
from ashare_multifactor.final_test.official_query_collector import (
    collect_official_query_coverage,
)
from ashare_multifactor.final_test.pipeline import resume_final_test_release
from ashare_multifactor.final_test.preparation import (
    prepare_final_test,
    verify_preparation,
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

    documents = commands.add_parser(
        "collect-documents",
        help="cache approved official documents for human review",
    )
    _add_common_arguments(documents)
    documents.add_argument("--output-root", type=Path, required=True)
    documents.add_argument("--max-documents", type=_positive_int)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
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
        result = collect_official_query_coverage(
            preparation=preparation,
            authorization=authorization,
            contract=load_action_source_contract(
                args.root / "configs/final_execution_sources.yaml"
            ),
            output_root=args.output_root,
            transport=UrllibOfficialQueryTransport(),
            policy=RetryPolicy(),
            max_scopes=args.max_scopes,
        )
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
        )
        print(workspace.review_queue_path)
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


if __name__ == "__main__":
    main()
