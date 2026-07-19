from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from test_final_test_resume import PreparedAttempt


pytest_plugins = ("test_final_test_resume",)


@dataclass(frozen=True)
class PreparedCli:
    attempt: PreparedAttempt
    approval_key_file: Path
    output_root: Path

    @property
    def argv(self) -> list[str]:
        return [
            "--root",
            str(self.attempt.code_root),
            "--data-root",
            str(self.attempt.data_root),
            "--approval-key-file",
            str(self.approval_key_file),
            "--attempt-id",
            self.attempt.attempt_id,
            "--output-root",
            str(self.output_root),
        ]


@pytest.fixture
def prepared_cli(prepared_attempt: PreparedAttempt, tmp_path: Path) -> PreparedCli:
    approval_key_file = tmp_path / "approval-key.bin"
    approval_key_file.write_bytes(prepared_attempt.approval_key)
    return PreparedCli(
        attempt=prepared_attempt,
        approval_key_file=approval_key_file,
        output_root=(
            prepared_attempt.data_root
            / "processed/final_test_evidence"
            / prepared_attempt.attempt_id
        ),
    )


class ZeroResultTransport:
    def fetch(
        self,
        endpoint: str,
        form: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> bytes:
        del endpoint, form, timeout_seconds
        return b'{"totalpages":0,"totalAnnouncement":0,"announcements":null}'


class FailIfCalled:
    def fetch(
        self,
        endpoint: str,
        form: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> bytes:
        del endpoint, form, timeout_seconds
        raise AssertionError("CLI must reject before starting collection")


class DocumentTransport:
    def fetch(self, url: str, *, timeout_seconds: float) -> bytes:
        del timeout_seconds
        return f"official document for {url}".encode()


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_collect_queries_uses_registered_attempt_without_new_token_consumption(
    prepared_cli: PreparedCli,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ashare_multifactor.cli import final_test

    attempts = prepared_cli.attempt.data_root / "processed/final_test/attempts"
    before = _tree_bytes(attempts)
    ledger_before = prepared_cli.attempt.consumption_ledger.read_bytes()
    monkeypatch.setattr(
        final_test,
        "UrllibOfficialQueryTransport",
        lambda: ZeroResultTransport(),
        raising=False,
    )

    final_test.main(["collect-queries", *prepared_cli.argv])

    assert _tree_bytes(attempts) == before
    assert prepared_cli.attempt.consumption_ledger.read_bytes() == ledger_before
    assert (
        prepared_cli.output_root
        / "official_query_coverage/official_query_coverage.json"
    ).is_file()
    assert capsys.readouterr().out.strip().endswith("official_query_coverage.json")


def test_collect_queries_rejects_foreign_output_before_network(
    prepared_cli: PreparedCli,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_multifactor.cli import final_test

    monkeypatch.setattr(
        final_test,
        "UrllibOfficialQueryTransport",
        lambda: FailIfCalled(),
        raising=False,
    )
    foreign = prepared_cli.attempt.data_root / "outside-evidence"
    argv = [
        "collect-queries",
        *prepared_cli.argv[:-1],
        str(foreign),
    ]

    with pytest.raises(ValueError, match="output root|attempt"):
        final_test.main(argv)


def test_collect_documents_keeps_attempt_and_ledger_bytes_unchanged(
    prepared_cli: PreparedCli,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ashare_multifactor.cli import final_test

    attempts = prepared_cli.attempt.data_root / "processed/final_test/attempts"
    before = _tree_bytes(attempts)
    ledger_before = prepared_cli.attempt.consumption_ledger.read_bytes()
    monkeypatch.setattr(
        final_test,
        "UrllibOfficialQueryTransport",
        lambda: ZeroResultTransport(),
        raising=False,
    )
    final_test.main(["collect-queries", *prepared_cli.argv])
    monkeypatch.setattr(
        final_test,
        "UrllibOfficialDocumentTransport",
        lambda: DocumentTransport(),
        raising=False,
    )

    final_test.main(["collect-documents", *prepared_cli.argv])

    assert _tree_bytes(attempts) == before
    assert prepared_cli.attempt.consumption_ledger.read_bytes() == ledger_before
    assert capsys.readouterr().out.strip().endswith("review_queue.parquet")
