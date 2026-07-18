from contextlib import contextmanager
from datetime import date
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from ashare_multifactor.final_test.official_query_coverage import (
    OFFICIAL_QUERY_ENDPOINT,
    OfficialQueryScope,
    canonical_json_bytes,
    page_request_sha256,
    validate_official_query_package,
)


def _scope() -> OfficialQueryScope:
    return OfficialQueryScope(
        symbol="000001",
        market="sz",
        category="corporate_actions",
        query_category="",
        start=date(2022, 1, 1),
        end=date(2025, 12, 31),
    )


def _write_package(
    tmp_path: Path,
    *,
    scope: OfficialQueryScope | None = None,
    endpoint: str = OFFICIAL_QUERY_ENDPOINT,
    method: str = "POST",
    pages: list[dict[str, object]] | None = None,
) -> Path:
    selected_scope = scope or _scope()
    root = tmp_path / "query-package"
    pages_root = root / "pages"
    pages_root.mkdir(parents=True)
    request = {
        "schema_version": "1",
        "endpoint": endpoint,
        "method": method,
        "scope": {
            "symbol": selected_scope.symbol,
            "market": selected_scope.market,
            "category": selected_scope.category,
            "query_category": selected_scope.query_category,
            "start": selected_scope.start.isoformat(),
            "end": selected_scope.end.isoformat(),
        },
        "page_size": 30,
        "form": {
            "category": selected_scope.query_category,
            "column": {"sh": "sse", "sz": "szse"}[selected_scope.market],
            "plate": selected_scope.market,
            "searchkey": "",
            "seDate": (f"{selected_scope.start.isoformat()}~{selected_scope.end.isoformat()}"),
            "stock": selected_scope.symbol,
            "tabName": "fulltext",
            "trade": "",
        },
    }
    (root / "request.json").write_bytes(canonical_json_bytes(request))
    raw_pages = pages or [
        {
            "totalpages": 1,
            "totalAnnouncement": 1,
            "announcements": [{"announcementId": "one"}],
        }
    ]
    records = []
    for page, payload in enumerate(raw_pages, start=1):
        cache_file = f"pages/page-{page:04d}.json"
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        (root / cache_file).write_bytes(encoded)
        records.append(
            {
                "page": page,
                "cache_file": cache_file,
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "size_bytes": len(encoded),
                "total_pages": payload["totalpages"],
                "total_results": payload["totalAnnouncement"],
                "request_sha256": page_request_sha256(request, page),
                "result_count": len(payload["announcements"] or []),
            }
        )
    pages_payload = {"schema_version": "1", "pages": records}
    pages_bytes = canonical_json_bytes(pages_payload)
    (root / "pages.json").write_bytes(pages_bytes)
    manifest = {
        "schema_version": "1",
        "request_sha256": hashlib.sha256(canonical_json_bytes(request)).hexdigest(),
        "pages_sha256": hashlib.sha256(pages_bytes).hexdigest(),
        "total_pages": records[0]["total_pages"],
        "total_results": records[0]["total_results"],
        "page_count": len(records),
        "completed": True,
        "created_at": "2026-07-18T00:00:00+00:00",
    }
    (root / "query_manifest.json").write_bytes(canonical_json_bytes(manifest))
    return root


def _rewrite_pages_and_manifest(root: Path, pages: dict[str, object]) -> None:
    pages_bytes = canonical_json_bytes(pages)
    (root / "pages.json").write_bytes(pages_bytes)
    manifest_path = root / "query_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pages_sha256"] = hashlib.sha256(pages_bytes).hexdigest()
    manifest_path.write_bytes(canonical_json_bytes(manifest))


def _rewrite_request_and_manifest(root: Path, request: dict[str, object]) -> None:
    request_bytes = canonical_json_bytes(request)
    (root / "request.json").write_bytes(request_bytes)
    manifest_path = root / "query_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["request_sha256"] = hashlib.sha256(request_bytes).hexdigest()
    manifest_path.write_bytes(canonical_json_bytes(manifest))


def test_query_package_validates_canonical_complete_post_evidence(
    tmp_path: Path,
) -> None:
    root = _write_package(tmp_path)

    verified = validate_official_query_package(root, expected_scope=_scope())

    assert verified.scope == _scope()
    assert verified.total_pages == 1
    assert verified.total_results == 1
    assert (
        verified.request_sha256 == hashlib.sha256((root / "request.json").read_bytes()).hexdigest()
    )


def test_query_package_accepts_a_supported_sh_market_scope(tmp_path: Path) -> None:
    scope = OfficialQueryScope(
        symbol="600000",
        market="sh",
        category="corporate_actions",
        query_category="",
        start=date(2022, 1, 1),
        end=date(2025, 12, 31),
    )

    verified = validate_official_query_package(
        _write_package(tmp_path, scope=scope),
        expected_scope=scope,
    )

    assert verified.scope == scope


def test_verified_query_identity_exposes_no_mutable_package_paths(
    tmp_path: Path,
) -> None:
    verified = validate_official_query_package(_write_package(tmp_path))

    assert not hasattr(verified, "root")
    assert not hasattr(verified, "response_paths")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("endpoint", "https://example.invalid/query"),
        ("method", "GET"),
    ],
)
def test_query_package_rejects_wrong_endpoint_or_method(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    root = _write_package(
        tmp_path,
        **{field: value},
    )

    with pytest.raises(ValueError, match="endpoint|method"):
        validate_official_query_package(root, expected_scope=_scope())


def test_query_package_rejects_cache_path_escape(
    tmp_path: Path,
) -> None:
    root = _write_package(tmp_path)
    pages = json.loads((root / "pages.json").read_text(encoding="utf-8"))
    pages["pages"][0]["cache_file"] = "../outside.json"
    _rewrite_pages_and_manifest(root, pages)

    with pytest.raises(ValueError, match="cache path"):
        validate_official_query_package(root, expected_scope=_scope())


def test_query_package_rejects_request_form_for_another_security(
    tmp_path: Path,
) -> None:
    root = _write_package(tmp_path)
    request = json.loads((root / "request.json").read_text(encoding="utf-8"))
    request["form"]["stock"] = "000002"
    _rewrite_request_and_manifest(root, request)

    with pytest.raises(ValueError, match="form.*symbol"):
        validate_official_query_package(root, expected_scope=_scope())


def test_query_package_rejects_request_form_for_another_market(
    tmp_path: Path,
) -> None:
    root = _write_package(tmp_path)
    request = json.loads((root / "request.json").read_text(encoding="utf-8"))
    request["form"]["column"] = "sse"
    _rewrite_request_and_manifest(root, request)

    with pytest.raises(ValueError, match="form.*market"):
        validate_official_query_package(root, expected_scope=_scope())


def test_query_package_rejects_request_form_with_another_date_range(
    tmp_path: Path,
) -> None:
    root = _write_package(tmp_path)
    request = json.loads((root / "request.json").read_text(encoding="utf-8"))
    request["form"]["seDate"] = "2021-01-01~2021-12-31"
    _rewrite_request_and_manifest(root, request)

    with pytest.raises(ValueError, match="form.*date"):
        validate_official_query_package(root, expected_scope=_scope())


def test_query_package_rejects_request_form_with_another_query_category(
    tmp_path: Path,
) -> None:
    root = _write_package(tmp_path)
    request = json.loads((root / "request.json").read_text(encoding="utf-8"))
    request["form"]["category"] = "category_other"
    _rewrite_request_and_manifest(root, request)

    with pytest.raises(ValueError, match="form.*category"):
        validate_official_query_package(root, expected_scope=_scope())


@pytest.mark.parametrize(
    "mutation",
    ["missing_page", "duplicate_page", "response_hash", "total_pages"],
)
def test_query_package_rejects_incomplete_or_inconsistent_pagination(
    tmp_path: Path,
    mutation: str,
) -> None:
    pages = [
        {
            "totalpages": 2,
            "totalAnnouncement": 2,
            "announcements": [{"announcementId": "one"}],
        },
        {
            "totalpages": 2,
            "totalAnnouncement": 2,
            "announcements": [{"announcementId": "two"}],
        },
    ]
    root = _write_package(tmp_path, pages=pages)
    pages_payload = json.loads((root / "pages.json").read_text(encoding="utf-8"))
    if mutation == "missing_page":
        pages_payload["pages"] = pages_payload["pages"][:1]
        manifest = json.loads((root / "query_manifest.json").read_text())
        manifest["page_count"] = 1
        (root / "query_manifest.json").write_bytes(canonical_json_bytes(manifest))
    elif mutation == "duplicate_page":
        pages_payload["pages"][1]["page"] = 1
        pages_payload["pages"][1]["cache_file"] = "pages/page-0001.json"
    elif mutation == "response_hash":
        pages_payload["pages"][1]["sha256"] = "0" * 64
    else:
        pages_payload["pages"][1]["total_pages"] = 3
    _rewrite_pages_and_manifest(root, pages_payload)
    if mutation == "missing_page":
        pages_bytes = canonical_json_bytes(pages_payload)
        manifest_path = root / "query_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["pages_sha256"] = hashlib.sha256(pages_bytes).hexdigest()
        manifest_path.write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(ValueError, match="page|pagination|hash|total"):
        validate_official_query_package(root, expected_scope=_scope())


def test_query_package_rejects_page_request_identity_drift(
    tmp_path: Path,
) -> None:
    root = _write_package(tmp_path)
    pages = json.loads((root / "pages.json").read_text(encoding="utf-8"))
    pages["pages"][0]["request_sha256"] = "0" * 64
    _rewrite_pages_and_manifest(root, pages)

    with pytest.raises(ValueError, match="page request"):
        validate_official_query_package(root, expected_scope=_scope())


def test_query_package_rejects_reused_response_on_another_page(
    tmp_path: Path,
) -> None:
    pages = [
        {
            "totalpages": 2,
            "totalAnnouncement": 2,
            "announcements": [{"announcementId": "one"}],
        },
        {
            "totalpages": 2,
            "totalAnnouncement": 2,
            "announcements": [{"announcementId": "two"}],
        },
    ]
    root = _write_package(tmp_path, pages=pages)
    first = root / "pages/page-0001.json"
    second = root / "pages/page-0002.json"
    second.write_bytes(first.read_bytes())
    pages_payload = json.loads((root / "pages.json").read_text(encoding="utf-8"))
    pages_payload["pages"][1]["sha256"] = hashlib.sha256(second.read_bytes()).hexdigest()
    pages_payload["pages"][1]["size_bytes"] = second.stat().st_size
    _rewrite_pages_and_manifest(root, pages_payload)

    with pytest.raises(ValueError, match="duplicate.*announcement"):
        validate_official_query_package(root, expected_scope=_scope())


def test_zero_result_query_requires_the_initial_official_response(
    tmp_path: Path,
) -> None:
    root = _write_package(
        tmp_path,
        pages=[
            {
                "totalpages": 0,
                "totalAnnouncement": 0,
                "announcements": None,
            }
        ],
    )

    verified = validate_official_query_package(root, expected_scope=_scope())

    assert verified.total_pages == 0
    assert verified.total_results == 0


def test_query_package_rejects_symbolic_page_cache(
    tmp_path: Path,
) -> None:
    root = _write_package(tmp_path)
    cache = root / "pages/page-0001.json"
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(cache.read_bytes())
    cache.unlink()
    cache.symlink_to(replacement)

    with pytest.raises(ValueError, match="unsafe|symlink"):
        validate_official_query_package(root, expected_scope=_scope())


def test_query_package_rejects_source_replacement_after_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _write_package(tmp_path)
    replacement = tmp_path / "replacement-package"
    displaced = tmp_path / "displaced-package"
    shutil.copytree(root, replacement)
    from ashare_multifactor.final_test import official_query_coverage

    original_validate = official_query_coverage._validate_package_tree

    def replace_source_after_snapshot(*args: object, **kwargs: object) -> object:
        root.rename(displaced)
        replacement.rename(root)
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(
        official_query_coverage,
        "_validate_package_tree",
        replace_source_after_snapshot,
    )

    with pytest.raises(ValueError, match="root.*identity"):
        validate_official_query_package(root, expected_scope=_scope())

    assert displaced.is_dir()
    assert root.is_dir()


def test_query_package_rejects_parent_replacement_after_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _write_package(tmp_path)
    replacement_parent = tmp_path.parent / "replacement-package-parent"
    displaced_parent = tmp_path.parent / "displaced-package-parent"
    shutil.copytree(tmp_path, replacement_parent)
    from ashare_multifactor.final_test import official_query_coverage

    original_validate = official_query_coverage._validate_package_tree

    def replace_parent_after_snapshot(*args: object, **kwargs: object) -> object:
        tmp_path.rename(displaced_parent)
        replacement_parent.rename(tmp_path)
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(
        official_query_coverage,
        "_validate_package_tree",
        replace_parent_after_snapshot,
    )

    with pytest.raises(ValueError, match="path.*identity"):
        validate_official_query_package(root, expected_scope=_scope())

    assert displaced_parent.is_dir()
    assert root.is_dir()


def test_query_package_keeps_returned_paths_bound_during_parent_aba_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _write_package(tmp_path)
    replacement_parent = tmp_path.parent / f"{tmp_path.name}-replacement"
    _write_package(
        replacement_parent,
        pages=[
            {
                "totalpages": 1,
                "totalAnnouncement": 1,
                "announcements": [{"announcementId": "replacement"}],
            }
        ],
    )
    displaced_parent = tmp_path.parent / f"{tmp_path.name}-displaced"
    replacement_after_swap = tmp_path.parent / f"{tmp_path.name}-replacement-after"
    from ashare_multifactor.final_test import official_query_coverage

    original_open = official_query_coverage.open_directory_at
    original_opened = official_query_coverage.opened_directory
    original_validate = official_query_coverage._validate_package_tree
    swapped = False

    def swap_parent() -> None:
        nonlocal swapped
        if swapped:
            return
        tmp_path.rename(displaced_parent)
        replacement_parent.rename(tmp_path)
        swapped = True

    @contextmanager
    def swap_before_text_parent_open(path: Path, *, label: str):
        if path == tmp_path:
            swap_parent()
        with original_opened(path, label=label) as descriptor:
            yield descriptor

    def swap_after_initial_parent_open(
        parent_fd: int,
        name: str,
        *,
        label: str,
    ) -> int:
        descriptor = original_open(parent_fd, name, label=label)
        if name == tmp_path.name:
            swap_parent()
        return descriptor

    def restore_original_before_snapshot_validation(
        *args: object,
        **kwargs: object,
    ) -> object:
        assert swapped
        tmp_path.rename(replacement_after_swap)
        displaced_parent.rename(tmp_path)
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(
        official_query_coverage,
        "open_directory_at",
        swap_after_initial_parent_open,
    )
    monkeypatch.setattr(
        official_query_coverage,
        "opened_directory",
        swap_before_text_parent_open,
    )
    monkeypatch.setattr(
        official_query_coverage,
        "_validate_package_tree",
        restore_original_before_snapshot_validation,
    )

    verified = validate_official_query_package(root, expected_scope=_scope())

    assert swapped
    assert verified.pages_sha256 == hashlib.sha256((root / "pages.json").read_bytes()).hexdigest()
