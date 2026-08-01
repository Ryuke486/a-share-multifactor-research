from __future__ import annotations

import ast
from dataclasses import replace
from datetime import date
import hashlib
from io import BytesIO
import json
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.final_test.official_candidate_pdf_evidence import (
    inspect_candidate_pdf,
)
from ashare_multifactor.final_test.official_candidate_review_rendition_validation import (
    CandidateReviewRenditionInput,
    _same_announcement_linkage,
    build_rendition_record,
    validate_rendition_record,
)
from ashare_multifactor.final_test.official_candidate_review_renditions import (
    bind_candidate_review_renditions,
    load_verified_candidate_review_renditions,
    publish_candidate_review_renditions,
)
from ashare_multifactor.final_test.official_candidate_review_rendition_authorization import (
    load_candidate_review_rendition_authorization,
)
from ashare_multifactor.final_test.official_document_validation import (
    validate_official_document,
)
from ashare_multifactor.final_test.official_candidate_review_admission import (
    require_candidate_review_admission,
)
from ashare_multifactor.final_test.official_evidence_workspace import (
    load_verified_review_queue,
)
from ashare_multifactor.final_test.official_query_coverage import (
    canonical_json_bytes,
)
from ashare_multifactor.final_test.official_review_submission import (
    load_verified_candidate_snapshot,
)
from test_final_test_official_candidate_review_admission import (
    _SplitRequirementAnnouncementTransport,
    _admission_inputs,
    _pdf_bytes,
    _rendition_authorization,
    _unicode_pdf_bytes,
    _write_canonical_json,
    _write_rendition_receipt,
)
from test_final_test_resume import PreparedAttempt


pytest_plugins = ("test_final_test_resume",)


def _valid_rendition(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
    *,
    drift_rendition_receipt: bool = False,
    rendition_attempt_count: int = 1,
    rendition_final_url: str | None = None,
    rendition_lines: list[str] | None = None,
    authority_lines: list[str] | None = None,
):
    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles={
            "000001": "关2023年度权益分派的公告",
            "600000": "2024年第三季度报告",
        },
        announcement_dates={
            "000001": date(2024, 5, 17),
            "600000": date(2024, 6, 18),
        },
        document_payloads={
            "000001": _unicode_pdf_bytes(
                [
                    "证券代码：000001",
                    "2023年年度权益分派实施公告",
                    "每10股派发现金红利1元（含税），不送红股，不转增股本。",
                    "除权除息日：2023-06-01",
                ]
            ),
            "600000": _pdf_bytes("2024-07-01"),
        },
        candidate_symbols={"000001"},
    )
    catalog_id = str(queue.frame.item(0, "catalog_id"))
    rendition_url = (
        "https://epaper.stcn.com/att/202405/17/"
        "ZQ17B093-XH_eBook.pdf"
    )
    authority_url = (
        "https://static.cninfo.com.cn/finalpage/2024-04-22/"
        "1219703216.PDF"
    )
    authorization_path, authorization = _rendition_authorization(
        prepared_attempt,
        workspace=workspace,
        queue=queue,
        candidates=candidates,
        tmp_path=tmp_path,
        rendition_source_url=rendition_url,
        authority_source_url=authority_url,
    )
    rendition_request = authorization.request("publisher_rendition")
    authority_request = authorization.request("publisher_authority")
    rendition_payload = _unicode_pdf_bytes(
        rendition_lines
        or [
            "证券代码：000001",
            "2023年年度权益分派实施公告",
            "每10股派发现金红利1元（含税），不送红股，不转增股本。",
            "除权除息日：2024-06-01",
        ]
    )
    authority_payload = _unicode_pdf_bytes(
        authority_lines
        or [
            "证券代码：000001",
            "公司指定信息披露媒体为《证券时报》及巨潮资讯网。",
        ]
    )
    rendition_request.document_path.parent.mkdir(parents=True)
    rendition_request.document_path.write_bytes(rendition_payload)
    authority_request.document_path.write_bytes(authority_payload)
    _write_rendition_receipt(
        rendition_request.receipt_path,
        source_url=rendition_url,
        payload=rendition_payload,
        authorization_sha256=authorization.sha256,
        request=rendition_request,
        attempt_count=rendition_attempt_count,
        final_url=rendition_final_url,
    )
    if drift_rendition_receipt:
        receipt = json.loads(rendition_request.receipt_path.read_bytes())
        receipt["request"]["request_sha256"] = "0" * 64
        rendition_request.receipt_path.write_bytes(
            canonical_json_bytes(receipt)
        )
    _write_rendition_receipt(
        authority_request.receipt_path,
        source_url=authority_url,
        payload=authority_payload,
        authorization_sha256=authorization.sha256,
        request=authority_request,
    )
    item = CandidateReviewRenditionInput(
        canonical_catalog_id=catalog_id,
        source_url=rendition_url,
        publication_date=date(2024, 5, 17),
        document_path=rendition_request.document_path,
        receipt_path=rendition_request.receipt_path,
        authority_source_url=authority_url,
        authority_publication_date=date(2024, 4, 22),
        authority_document_path=authority_request.document_path,
        authority_receipt_path=authority_request.receipt_path,
    )
    renditions = publish_candidate_review_renditions(
        workspace=workspace,
        queue=queue,
        network_authorization_path=authorization_path,
        renditions=(item,),
    )
    return workspace, queue, authorization, renditions


def test_candidate_rendition_closes_admission_in_new_review_session(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    workspace, queue, _, renditions = _valid_rendition(
        prepared_attempt,
        tmp_path,
    )
    bound_workspace = bind_candidate_review_renditions(
        workspace=workspace,
        queue=queue,
        renditions=renditions,
    )
    bound_queue = load_verified_review_queue(bound_workspace)
    admission = require_candidate_review_admission(
        workspace=bound_workspace,
        queue=bound_queue,
        candidates=load_verified_candidate_snapshot(
            workspace.root.parent
            / "corporate_action_candidate_collection/snapshot/manifest.json",
            workspace=bound_workspace,
        ),
        date_rule_path=(
            Path(__file__).parents[1]
            / "configs/evidence/"
            "stage9_candidate_review_admission_date_rule.json"
        ),
    )

    assert admission.ready is True
    assert admission.manifest["evidence_renditions"]["manifest_sha256"] == (
        renditions.manifest_sha256
    )
    copied = bound_workspace.review_queue_path.parent.parent / ("f" * 64)
    copied.mkdir()
    for source in bound_workspace.review_queue_path.parent.iterdir():
        (copied / source.name).write_bytes(source.read_bytes())
    with pytest.raises(ValueError, match="session identity"):
        load_verified_review_queue(
            replace(
                bound_workspace,
                review_queue_path=copied / "review_queue.parquet",
            )
        )


def test_candidate_rendition_review_session_identity_binds_base_queue(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    workspace, queue, _, renditions = _valid_rendition(
        prepared_attempt,
        tmp_path,
    )
    bound_workspace = bind_candidate_review_renditions(
        workspace=workspace,
        queue=queue,
        renditions=renditions,
    )
    manifest = json.loads(
        bound_workspace.review_queue_path.with_name(
            "review_manifest.json"
        ).read_bytes()
    )
    expected = hashlib.sha256(
        canonical_json_bytes(
            {
                "base_review_manifest_sha256": (
                    manifest["base_review_manifest"]["sha256"]
                ),
                "base_review_queue_sha256": (
                    manifest["review_queue"]["sha256"]
                ),
                "rendition_manifest_sha256": (
                    manifest["candidate_review_renditions"][
                        "manifest_sha256"
                    ]
                ),
            }
        )
    ).hexdigest()

    assert bound_workspace.review_queue_path.parent.name == expected


def test_candidate_rendition_binding_rejects_v3_as_base_without_publishing(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    workspace, queue, _, renditions = _valid_rendition(
        prepared_attempt,
        tmp_path,
    )
    bound_workspace = bind_candidate_review_renditions(
        workspace=workspace,
        queue=queue,
        renditions=renditions,
    )
    bound_queue = load_verified_review_queue(bound_workspace)
    sessions_root = bound_workspace.review_queue_path.parent.parent
    before = {path.name for path in sessions_root.iterdir()}

    with pytest.raises(ValueError, match="base review queue"):
        bind_candidate_review_renditions(
            workspace=bound_workspace,
            queue=bound_queue,
            renditions=renditions,
        )

    assert {path.name for path in sessions_root.iterdir()} == before


def test_candidate_rendition_review_session_recursively_verifies_base_queue(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    workspace, queue, _, renditions = _valid_rendition(
        prepared_attempt,
        tmp_path,
    )
    bound_workspace = bind_candidate_review_renditions(
        workspace=workspace,
        queue=queue,
        renditions=renditions,
    )
    base_queue_path = queue.manifest_path.with_name("review_queue.parquet")
    base_queue_path.write_bytes(base_queue_path.read_bytes() + b"drift")

    with pytest.raises(ValueError, match="base official evidence review"):
        load_verified_review_queue(bound_workspace)


def test_candidate_rendition_review_session_queue_equals_base_bytes(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    workspace, queue, _, renditions = _valid_rendition(
        prepared_attempt,
        tmp_path,
    )
    bound_workspace = bind_candidate_review_renditions(
        workspace=workspace,
        queue=queue,
        renditions=renditions,
    )
    queue_path = bound_workspace.review_queue_path
    frame = pl.read_parquet(queue_path).with_columns(
        pl.when(pl.col("catalog_id") == pl.col("catalog_id").first())
        .then(pl.lit("drifted title"))
        .otherwise(pl.col("announcement_title"))
        .alias("announcement_title")
    )
    stream = BytesIO()
    frame.write_parquet(stream, compression="zstd")
    queue_bytes = stream.getvalue()
    queue_path.write_bytes(queue_bytes)
    manifest_path = queue_path.with_name("review_manifest.json")
    manifest = json.loads(manifest_path.read_bytes())
    manifest["review_queue"] = {
        **manifest["review_queue"],
        "sha256": hashlib.sha256(queue_bytes).hexdigest(),
        "size_bytes": len(queue_bytes),
    }
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(ValueError, match="base review queue"):
        load_verified_review_queue(bound_workspace)


def test_candidate_rendition_review_session_inherits_base_manifest_fields(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    workspace, queue, _, renditions = _valid_rendition(
        prepared_attempt,
        tmp_path,
    )
    bound_workspace = bind_candidate_review_renditions(
        workspace=workspace,
        queue=queue,
        renditions=renditions,
    )
    manifest_path = bound_workspace.review_queue_path.with_name(
        "review_manifest.json"
    )
    manifest = json.loads(manifest_path.read_bytes())
    manifest["documents"] = []
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(ValueError, match="base review manifest"):
        load_verified_review_queue(bound_workspace)


@pytest.mark.parametrize(
    "rendition_lines,authority_lines",
    [
        pytest.param(
            [
                "证券代码：600000",
                "2023年年度权益分派实施公告",
                "每10股派发现金红利1元（含税），不送红股，不转增股本。",
                "除权除息日：2024-06-01",
            ],
            [
                "证券代码：000001",
                "公司指定信息披露媒体为《证券时报》及巨潮资讯网。",
            ],
            id="cross-security",
        ),
        pytest.param(
            [
                "证券代码：000001",
                "2023年年度权益分派实施公告",
                "每10股派发现金红利1元（含税），不送红股，不转增股本。",
                "2024-06-01",
            ],
            [
                "证券代码：000001",
                "公司指定信息披露媒体为《证券时报》及巨潮资讯网。",
            ],
            id="missing-date-label",
        ),
        pytest.param(
            [
                "证券代码：000001",
                "2023年年度权益分派实施公告",
                "每10股派发现金红利2元（含税），不送红股，不转增股本。",
                "除权除息日：2024-06-01",
            ],
            [
                "证券代码：000001",
                "公司指定信息披露媒体为《证券时报》及巨潮资讯网。",
            ],
            id="different-distribution",
        ),
        pytest.param(
            [
                "证券代码：000001",
                "2023年年度权益分派实施公告",
                "每10股派发现金红利1元（含税），不送红股，不转增股本。",
                "除权除息日：2024-06-01",
            ],
            [
                "证券代码：000001",
                "公司发布年度报告。",
                "证券时报刊载了市场新闻。",
                "巨潮资讯网提供网站服务。",
            ],
            id="authority-keywords-scattered",
        ),
    ],
)
def test_candidate_rendition_rejects_split_or_cross_document_evidence(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
    rendition_lines: list[str],
    authority_lines: list[str],
) -> None:
    with pytest.raises(ValueError):
        _valid_rendition(
            prepared_attempt,
            tmp_path,
            rendition_lines=rendition_lines,
            authority_lines=authority_lines,
        )


def test_candidate_rendition_rejects_arbitrary_authorization_file(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    workspace, queue, _ = _admission_inputs(
        prepared_attempt,
        titles={
            "000001": "关于2023年度权益分派的公告",
            "600000": "2024年第三季度报告",
        },
        announcement_dates={
            "000001": date(2024, 5, 17),
            "600000": date(2024, 6, 18),
        },
        document_payloads={
            "000001": _pdf_bytes("2023-06-01"),
            "600000": _pdf_bytes("2024-07-01"),
        },
        candidate_symbols={"000001"},
    )
    authorization_path = tmp_path / "arbitrary.json"
    _write_canonical_json(authorization_path, {"role": "authorization"})

    with pytest.raises(ValueError, match="authorization"):
        publish_candidate_review_renditions(
            workspace=workspace,
            queue=queue,
            network_authorization_path=authorization_path,
            renditions=(
                CandidateReviewRenditionInput(
                    canonical_catalog_id=str(queue.frame.item(0, "catalog_id")),
                    source_url=(
                        "https://epaper.stcn.com/att/202405/17/"
                        "ZQ17B093-XH_eBook.pdf"
                    ),
                    publication_date=date(2024, 5, 17),
                    document_path=tmp_path / "missing.pdf",
                    receipt_path=tmp_path / "missing.receipt.json",
                    authority_source_url=(
                        "https://static.cninfo.com.cn/finalpage/2024-04-22/"
                        "1219703216.PDF"
                    ),
                    authority_publication_date=date(2024, 4, 22),
                    authority_document_path=tmp_path / "missing-authority.pdf",
                    authority_receipt_path=(
                        tmp_path / "missing-authority.receipt.json"
                    ),
                ),
            ),
        )


def test_candidate_rendition_authorization_rejects_unclassified_topology(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles={
            "000001": "2024年度权益分派实施公告",
            "600000": "2024年第三季度报告",
        },
        announcement_dates={
            "000001": date(2024, 6, 2),
            "600000": date(2024, 6, 18),
        },
        document_payloads={
            "000001": _unicode_pdf_bytes(
                [
                    "证券代码：000001",
                    "2024年度权益分派实施公告",
                    "除权除息日：2024-06-01",
                ]
            ),
            "600000": _pdf_bytes("2024-07-01"),
        },
        candidate_symbols={"000001"},
    )

    with pytest.raises(ValueError, match="topology"):
        _rendition_authorization(
            prepared_attempt,
            workspace=workspace,
            queue=queue,
            candidates=candidates,
            tmp_path=tmp_path,
            rendition_source_url=(
                "https://epaper.stcn.com/att/202406/02/"
                "ZQ02B001-XH_eBook.pdf"
            ),
            authority_source_url=(
                "https://static.cninfo.com.cn/finalpage/2024-04-22/"
                "1219703216.PDF"
            ),
        )


def test_candidate_rendition_authorization_allows_other_query_partition(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles={
            "000001": "关2023年度权益分派的公告",
            "600000": "2024年第三季度报告",
        },
        announcement_dates={
            "000001": date(2024, 5, 17),
            "600000": date(2024, 6, 18),
        },
        document_payloads={
            "000001": _unicode_pdf_bytes(
                [
                    "证券代码：000001",
                    "2023年年度权益分派实施公告",
                    "除权除息日：2023-06-01",
                ]
            ),
            "600000": _pdf_bytes("2024-07-01"),
        },
        candidate_symbols={"000001", "600000"},
    )
    candidate_id = str(
        candidates.candidates.filter(pl.col("symbol") == "000001").item(
            0,
            "candidate_id",
        )
    )

    _, authorization = _rendition_authorization(
        prepared_attempt,
        workspace=workspace,
        queue=queue,
        candidates=candidates,
        tmp_path=tmp_path,
        candidate_id=candidate_id,
        rendition_source_url=(
            "https://epaper.stcn.com/att/202405/17/"
            "ZQ17B093-XH_eBook.pdf"
        ),
        authority_source_url=(
            "https://static.cninfo.com.cn/finalpage/2024-04-22/"
            "1219703216.PDF"
        ),
    )

    assert authorization.candidate_id == candidate_id


def test_candidate_rendition_authorization_rejects_two_eligible_anchors(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    titles = {
        "000001": "2024年度权益分派实施公告",
        "600000": "2024年第三季度报告",
    }
    dates = {
        "000001": date(2024, 5, 28),
        "600000": date(2024, 6, 18),
    }
    wrong_date_pdf = _unicode_pdf_bytes(
        [
            "证券代码：000001",
            "2024年度权益分派实施公告",
            "除权除息日：2023-06-01",
        ]
    )
    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles=titles,
        announcement_dates=dates,
        document_payloads={
            "000001-prior": wrong_date_pdf,
            "000001-post": wrong_date_pdf,
            "600000": _pdf_bytes("2024-07-01"),
        },
        candidate_symbols={"000001"},
        announcement_transport=_SplitRequirementAnnouncementTransport(
            titles=titles,
            announcement_dates=dates,
        ),
    )

    with pytest.raises(ValueError, match="anchor"):
        _rendition_authorization(
            prepared_attempt,
            workspace=workspace,
            queue=queue,
            candidates=candidates,
            tmp_path=tmp_path,
            rendition_source_url=(
                "https://epaper.stcn.com/att/202405/28/"
                "ZQ28B001-XH_eBook.pdf"
            ),
            authority_source_url=(
                "https://static.cninfo.com.cn/finalpage/2024-04-22/"
                "1219703216.PDF"
            ),
        )


def test_candidate_rendition_authorization_rejects_same_day_authority(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles={
            "000001": "2023年年度权益分派实施公告",
            "600000": "2024年第三季度报告",
        },
        announcement_dates={
            "000001": date(2024, 5, 17),
            "600000": date(2024, 6, 18),
        },
        document_payloads={
            "000001": _unicode_pdf_bytes(
                [
                    "证券代码：000001",
                    "2023年年度权益分派实施公告",
                    "除权除息日：2023-06-01",
                ]
            ),
            "600000": _pdf_bytes("2024-07-01"),
        },
        candidate_symbols={"000001"},
    )

    with pytest.raises(ValueError, match="date"):
        _rendition_authorization(
            prepared_attempt,
            workspace=workspace,
            queue=queue,
            candidates=candidates,
            tmp_path=tmp_path,
            rendition_source_url=(
                "https://epaper.stcn.com/att/202405/17/"
                "ZQ17B093-XH_eBook.pdf"
            ),
            authority_source_url=(
                "https://static.cninfo.com.cn/finalpage/2024-05-17/"
                "1219703216.PDF"
            ),
        )


def test_candidate_rendition_publish_rejects_same_day_authority(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    workspace, queue, authorization, _ = _valid_rendition(
        prepared_attempt,
        tmp_path,
    )
    anchored = queue.frame.filter(
        pl.col("catalog_id") == authorization.canonical_catalog_id
    ).to_dicts()[0]
    rendition_request = authorization.request("publisher_rendition")
    original_authority = authorization.request("publisher_authority")
    same_day_url = (
        "https://static.cninfo.com.cn/finalpage/2024-05-17/"
        "1219703216.PDF"
    )
    authority_request = replace(
        original_authority,
        source_url=same_day_url,
        request_sha256="f" * 64,
    )
    authority_payload = authority_request.document_path.read_bytes()
    _write_rendition_receipt(
        authority_request.receipt_path,
        source_url=same_day_url,
        payload=authority_payload,
        authorization_sha256=authorization.sha256,
        request=authority_request,
    )
    item = CandidateReviewRenditionInput(
        canonical_catalog_id=authorization.canonical_catalog_id,
        source_url=rendition_request.source_url,
        publication_date=date(2024, 5, 17),
        document_path=rendition_request.document_path,
        receipt_path=rendition_request.receipt_path,
        authority_source_url=same_day_url,
        authority_publication_date=date(2024, 5, 17),
        authority_document_path=authority_request.document_path,
        authority_receipt_path=authority_request.receipt_path,
    )

    with pytest.raises(ValueError, match="pre-event"):
        build_rendition_record(
            item,
            anchored=anchored,
            canonical_payload=(
                workspace.root / str(anchored["document_cache_path"])
            ).read_bytes(),
            authorization_sha256=authorization.sha256,
            rendition_request=rendition_request,
            authority_request=authority_request,
        )


def test_candidate_rendition_replay_rejects_same_day_authority(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    workspace, queue, authorization, renditions = _valid_rendition(
        prepared_attempt,
        tmp_path,
    )
    anchored = queue.frame.filter(
        pl.col("catalog_id") == authorization.canonical_catalog_id
    ).to_dicts()[0]
    record = json.loads(json.dumps(renditions.manifest["records"][0]))
    files = {
        path.relative_to(renditions.root).as_posix(): path.read_bytes()
        for path in renditions.root.rglob("*")
        if path.is_file() and path.name != "rendition_manifest.json"
    }
    same_day_url = (
        "https://static.cninfo.com.cn/finalpage/2024-05-17/"
        "1219703216.PDF"
    )
    original_authority = authorization.request("publisher_authority")
    authority_request = replace(
        original_authority,
        source_url=same_day_url,
        request_sha256="f" * 64,
    )
    authority_payload = authority_request.document_path.read_bytes()
    receipt_path = tmp_path / "same-day-authority.receipt.json"
    _write_rendition_receipt(
        receipt_path,
        source_url=same_day_url,
        payload=authority_payload,
        authorization_sha256=authorization.sha256,
        request=authority_request,
    )
    receipt_bytes = receipt_path.read_bytes()
    receipt_sha = hashlib.sha256(receipt_bytes).hexdigest()
    old_receipt_path = str(record["authority"]["receipt"]["path"])
    files.pop(old_receipt_path)
    new_receipt_path = f"receipts/{receipt_sha}.json"
    files[new_receipt_path] = receipt_bytes
    record["authority"]["source_url"] = same_day_url
    record["authority"]["publication_date"] = "2024-05-17"
    record["authority"]["receipt"] = {
        "path": new_receipt_path,
        "sha256": receipt_sha,
        "size_bytes": len(receipt_bytes),
    }

    with pytest.raises(ValueError, match="pre-event"):
        validate_rendition_record(
            record,
            files=files,
            anchored=anchored,
            canonical_payload=(
                workspace.root / str(anchored["document_cache_path"])
            ).read_bytes(),
            authorization_sha256=authorization.sha256,
            rendition_request=authorization.request("publisher_rendition"),
            authority_request=authority_request,
        )


@pytest.mark.parametrize(
    "lineage_key",
    [
        "parent_authorization",
        "attempt_state",
        "token_snapshot",
        "consumed_opening_ledger",
        "release_manifest",
        "release_lineage",
        "seal",
        "preparation",
    ],
)
def test_candidate_rendition_authorization_replays_every_lineage_leaf(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
    lineage_key: str,
) -> None:
    workspace, queue, authorization, _ = _valid_rendition(
        prepared_attempt,
        tmp_path,
    )
    authorization_path = tmp_path / "rendition_authorization.json"
    binding = authorization.payload["lineage"][lineage_key]
    assert isinstance(binding, dict)
    leaf = Path(str(binding["path"]))
    leaf.write_bytes(leaf.read_bytes() + b" ")

    with pytest.raises(ValueError):
        load_candidate_review_rendition_authorization(
            authorization_path,
            workspace=workspace,
            queue=queue,
        )


def test_candidate_rendition_receipt_binds_exact_authorized_request(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="receipt"):
        _valid_rendition(
            prepared_attempt,
            tmp_path,
            drift_rendition_receipt=True,
        )


def test_candidate_rendition_receipt_accepts_success_on_second_attempt(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    _, _, _, renditions = _valid_rendition(
        prepared_attempt,
        tmp_path,
        rendition_attempt_count=2,
    )

    assert renditions.manifest["record_count"] == 1
    record = renditions.manifest["records"][0]
    receipt = json.loads(
        (renditions.root / record["receipt"]["path"]).read_bytes()
    )
    assert receipt["request"]["attempt_count"] == 2
    assert receipt["request"]["final_url"] == record["source_url"]
    assert "final_url_identical" not in receipt["request"]


@pytest.mark.parametrize("attempt_count", [0, 4, True])
def test_candidate_rendition_receipt_rejects_invalid_attempt_count(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
    attempt_count: int,
) -> None:
    with pytest.raises(ValueError, match="receipt"):
        _valid_rendition(
            prepared_attempt,
            tmp_path,
            rendition_attempt_count=attempt_count,
        )


def test_candidate_rendition_receipt_rejects_final_url_drift(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="receipt"):
        _valid_rendition(
            prepared_attempt,
            tmp_path,
            rendition_final_url=(
                "https://epaper.stcn.com/att/202405/17/"
                "UNAUTHORIZED_eBook.pdf"
            ),
        )


def test_candidate_rendition_replay_rejects_content_addressed_weak_title(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    workspace, queue, authorization, renditions = _valid_rendition(
        prepared_attempt,
        tmp_path,
    )
    files = {
        path.relative_to(renditions.root).as_posix(): path.read_bytes()
        for path in renditions.root.rglob("*")
        if path.is_file()
    }
    manifest = json.loads(files["rendition_manifest.json"])
    record = manifest["records"][0]
    old_document_path = record["document"]["path"]
    old_receipt_path = record["receipt"]["path"]
    weak_payload = _unicode_pdf_bytes(
        [
            "证券代码：000001",
            "关于2023年度权益分派的公告",
            "每10股派发现金红利1元（含税），不送红股，不转增股本。",
            "除权除息日：2024-06-01",
        ]
    )
    validated = validate_official_document(
        weak_payload,
        source_url=str(record["source_url"]),
    )
    document_record = {
        "path": f"documents/{validated.sha256}/document.bin",
        "sha256": validated.sha256,
        "size_bytes": validated.size_bytes,
        "page_count": validated.page_count,
        "media_type": validated.media_type,
    }
    request = authorization.request("publisher_rendition")
    weak_receipt_path = tmp_path / "weak.receipt.json"
    _write_rendition_receipt(
        weak_receipt_path,
        source_url=request.source_url,
        payload=weak_payload,
        authorization_sha256=authorization.sha256,
        request=request,
    )
    weak_receipt = weak_receipt_path.read_bytes()
    receipt_sha256 = hashlib.sha256(weak_receipt).hexdigest()
    receipt_record = {
        "path": f"receipts/{receipt_sha256}.json",
        "sha256": receipt_sha256,
        "size_bytes": len(weak_receipt),
    }
    anchor = queue.frame.row(0, named=True)
    canonical_payload = (
        workspace.root / str(anchor["document_cache_path"])
    ).read_bytes()
    evidence = inspect_candidate_pdf(weak_payload)
    record["document"] = document_record
    record["receipt"] = receipt_record
    record["strong_reason"] = None
    record["labelled_dates"] = [
        value.isoformat() for value in evidence.labelled_dates
    ]
    record["same_announcement_linkage"] = _same_announcement_linkage(
        inspect_candidate_pdf(canonical_payload),
        evidence,
        symbol="000001",
        publication_date=date(2024, 5, 17),
    )
    del files[old_document_path]
    del files[old_receipt_path]
    files[document_record["path"]] = weak_payload
    files[receipt_record["path"]] = weak_receipt
    manifest_bytes = canonical_json_bytes(manifest)
    files["rendition_manifest.json"] = manifest_bytes
    weak_id = hashlib.sha256(manifest_bytes).hexdigest()
    weak_root = renditions.root.parent / weak_id
    for name, payload in files.items():
        destination = weak_root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)

    with pytest.raises(ValueError, match="document"):
        load_verified_candidate_review_renditions(
            weak_root / "rendition_manifest.json",
            workspace=workspace,
            queue=queue,
        )


def test_rendition_authorization_module_has_no_transport_dependency() -> None:
    source = (
        Path(__file__).parents[1]
        / "src/ashare_multifactor/final_test/"
        "official_candidate_review_rendition_authorization.py"
    )
    imports = {
        node.module
        for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, ast.ImportFrom)
    }
    assert all(
        "transport" not in str(module)
        and "official_query_client" not in str(module)
        for module in imports
    )
