"""CLI boundary for resumable official-evidence review batches."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any

import polars as pl

from ashare_multifactor.final_test.official_evidence_workspace import (
    EvidenceWorkspace,
)
from ashare_multifactor.final_test.official_review_batch_workspace import (
    prepare_review_batch_workspace,
)
from ashare_multifactor.final_test.official_review_batches import (
    finalize_review_batches,
    publish_review_batch,
)


REVIEW_BATCH_COMMANDS = frozenset(
    {
        "prepare-review-batches",
        "publish-review-batch",
        "finalize-review-batches",
    }
)


def add_review_batch_commands(
    commands: Any,
    *,
    add_common_arguments: Callable[[argparse.ArgumentParser], None],
    positive_int: Callable[[str], int],
) -> None:
    """Register the three-step prepare, publish, and finalize interface."""
    review_plan = commands.add_parser(
        "prepare-review-batches",
        help="freeze deterministic symbol-sharded inputs for resumable review",
    )
    add_common_arguments(review_plan)
    _add_workspace_arguments(review_plan)
    review_plan.add_argument(
        "--symbols-per-batch",
        type=positive_int,
        required=True,
    )

    review_batch = commands.add_parser(
        "publish-review-batch",
        help="validate and freeze one review batch",
    )
    add_common_arguments(review_batch)
    _add_workspace_arguments(review_batch)
    review_batch.add_argument("--batch-manifest", type=Path, required=True)
    review_batch.add_argument("--announcement-decisions", type=Path, required=True)
    review_batch.add_argument("--corporate-dispositions", type=Path, required=True)
    review_batch.add_argument("--corporate-facts", type=Path, required=True)
    review_batch.add_argument("--security-facts", type=Path, required=True)
    _add_reviewer_arguments(review_batch)

    review_finalize = commands.add_parser(
        "finalize-review-batches",
        help="merge every immutable batch through the full review gate",
    )
    add_common_arguments(review_finalize)
    _add_workspace_arguments(review_finalize)
    review_finalize.add_argument(
        "--batch-plan-manifest",
        type=Path,
        required=True,
    )
    _add_reviewer_arguments(review_finalize)


def run_review_batch_command(
    args: argparse.Namespace,
    *,
    workspace: EvidenceWorkspace,
) -> tuple[str, ...]:
    """Execute one already-authorized batch command and return display lines."""
    if args.command == "prepare-review-batches":
        result = prepare_review_batch_workspace(
            workspace=workspace,
            candidate_manifest_path=args.candidate_manifest,
            symbols_per_batch=args.symbols_per_batch,
        )
        return str(result.manifest_path), f"batch_count={len(result.batches)}"
    if args.command == "publish-review-batch":
        result = publish_review_batch(
            workspace=workspace,
            candidate_manifest_path=args.candidate_manifest,
            batch_manifest_path=args.batch_manifest,
            announcement_decisions=pl.read_parquet(args.announcement_decisions),
            corporate_action_dispositions=pl.read_parquet(
                args.corporate_dispositions
            ),
            corporate_action_facts=pl.read_parquet(args.corporate_facts),
            security_event_facts=pl.read_parquet(args.security_facts),
            reviewer_id=args.reviewer_id,
            reviewed_at=args.reviewed_at,
        )
        return (str(result.manifest_path),)
    if args.command == "finalize-review-batches":
        result = finalize_review_batches(
            workspace=workspace,
            candidate_manifest_path=args.candidate_manifest,
            batch_workspace_manifest_path=args.batch_plan_manifest,
            reviewer_id=args.reviewer_id,
            reviewed_at=args.reviewed_at,
        )
        return (str(result.manifest_path),)
    raise ValueError("unknown review batch command")


def _add_workspace_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--review-queue", type=Path, required=True)
    parser.add_argument("--candidate-manifest", type=Path, required=True)


def _add_reviewer_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--reviewer-id", required=True)
    parser.add_argument("--reviewed-at", required=True)
