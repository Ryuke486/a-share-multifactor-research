from __future__ import annotations

import argparse
import json
from pathlib import Path

from ashare_multifactor.robustness.evidence_workflow_rehearsal import (
    build_evidence_workflow_rehearsal,
)
from ashare_multifactor.robustness.evidence_workflow_successor_release import (
    publish_evidence_workflow_successor_release,
)
from ashare_multifactor.robustness.pipeline import (
    execute_robustness_reproducibility,
    execute_robustness_run,
    publish_robustness_release,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run sealed Stage-8 robustness analysis")
    parser.add_argument(
        "command",
        choices=(
            "run",
            "reproduce",
            "publish",
            "rehearse-evidence",
            "publish-evidence-successor",
        ),
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--run-id")
    parser.add_argument("--action-audit-root", type=Path)
    parser.add_argument("--collector-readiness-root", type=Path)
    parser.add_argument("--evidence-workflow-readiness-root", type=Path)
    parser.add_argument("--change-impact-audit", type=Path)
    parser.add_argument("--junit-report", type=Path)
    parser.add_argument("--readiness-output-root", type=Path)
    args = parser.parse_args()
    if args.command == "rehearse-evidence":
        required = {
            "--collector-readiness-root": args.collector_readiness_root,
            "--junit-report": args.junit_report,
            "--readiness-output-root": args.readiness_output_root,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            parser.error("rehearse-evidence requires " + ", ".join(missing))
        result = build_evidence_workflow_rehearsal(
            code_root=args.root,
            collector_readiness_root=args.collector_readiness_root,
            junit_path=args.junit_report,
            output_root=args.readiness_output_root,
        )
        print(result)
    elif args.command == "run":
        result = execute_robustness_run(args.root, run_id=args.run_id)
        print(result)
    elif args.command == "reproduce":
        result = execute_robustness_reproducibility(
            args.root, run_id_prefix=args.run_id
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    elif args.command == "publish":
        if not args.run_id:
            parser.error("publish requires --run-id")
        result = publish_robustness_release(args.root, run_id=args.run_id)
        print(result.root)
    else:
        required = {
            "--run-id": args.run_id,
            "--action-audit-root": args.action_audit_root,
            "--collector-readiness-root": args.collector_readiness_root,
            "--evidence-workflow-readiness-root": (
                args.evidence_workflow_readiness_root
            ),
            "--change-impact-audit": args.change_impact_audit,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            parser.error(
                "publish-evidence-successor requires " + ", ".join(missing)
            )
        result = publish_evidence_workflow_successor_release(
            args.root,
            run_id=args.run_id,
            action_audit_root=args.action_audit_root,
            collector_readiness_root=args.collector_readiness_root,
            evidence_workflow_readiness_root=args.evidence_workflow_readiness_root,
            change_impact_audit_path=args.change_impact_audit,
        )
        print(result.root)


if __name__ == "__main__":
    main()
