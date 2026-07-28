from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import signal
from types import SimpleNamespace

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
        del timeout_seconds
        if endpoint.endswith("/information/topSearch/query"):
            symbol = form["keyWord"]
            return json.dumps(
                [{"code": symbol, "orgId": f"fixture-{symbol}"}],
                separators=(",", ":"),
            ).encode()
        return b'{"totalpages":0,"totalAnnouncement":0,"announcements":null}'


class HeartbeatInspectingTransport(ZeroResultTransport):
    def __init__(self, heartbeat_path: Path) -> None:
        self.heartbeat_path = heartbeat_path
        self.snapshots: list[dict[str, object] | None] = []

    def fetch(
        self,
        endpoint: str,
        form: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> bytes:
        snapshot = (
            json.loads(self.heartbeat_path.read_bytes())
            if self.heartbeat_path.is_file()
            else None
        )
        self.snapshots.append(snapshot)
        return super().fetch(
            endpoint,
            form,
            timeout_seconds=timeout_seconds,
        )


class TransientQueryInterruptionTransport(ZeroResultTransport):
    def __init__(self) -> None:
        self.query_attempts = 0

    def fetch(
        self,
        endpoint: str,
        form: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> bytes:
        if not endpoint.endswith("/information/topSearch/query"):
            from ashare_multifactor.final_test.official_query_client import (
                OfficialQueryTransientError,
            )

            self.query_attempts += 1
            if self.query_attempts <= 3:
                raise OfficialQueryTransientError("injected connection reset")
        return super().fetch(
            endpoint,
            form,
            timeout_seconds=timeout_seconds,
        )


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


def test_collection_monitor_reads_collector_heartbeat_without_approval_key(
    prepared_cli: PreparedCli,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ashare_multifactor.cli import final_test

    monkeypatch.setattr(
        final_test,
        "UrllibOfficialQueryTransport",
        lambda: ZeroResultTransport(),
        raising=False,
    )
    final_test.main(["collect-queries", *prepared_cli.argv])
    capsys.readouterr()

    final_test.main(
        [
            "monitor-collection",
            "--data-root",
            str(prepared_cli.attempt.data_root),
            "--attempt-id",
            prepared_cli.attempt.attempt_id,
            "--output-root",
            str(prepared_cli.output_root),
        ]
    )

    output = capsys.readouterr().out.strip()
    assert output.startswith("green |")
    assert "2/2" in output


def test_collector_updates_heartbeat_during_identity_and_query_requests(
    prepared_cli: PreparedCli,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_multifactor.cli import final_test

    heartbeat = prepared_cli.output_root / "collection_heartbeat.json"
    transport = HeartbeatInspectingTransport(heartbeat)
    monkeypatch.setattr(
        final_test,
        "UrllibOfficialQueryTransport",
        lambda: transport,
        raising=False,
    )

    final_test.main(["collect-queries", *prepared_cli.argv])

    snapshots = [snapshot for snapshot in transport.snapshots if snapshot is not None]
    assert snapshots
    assert snapshots[0]["phase"] == "resolve_identities"
    assert any(snapshot["phase"] == "collect_queries" for snapshot in snapshots)
    assert all(
        snapshot["identity"]["attempt_id"] == prepared_cli.attempt.attempt_id
        for snapshot in snapshots
    )


def test_collect_queries_revalidates_identity_before_one_outer_network_recovery(
    prepared_cli: PreparedCli,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_multifactor.cli import final_test
    from ashare_multifactor.final_test.official_query_client import RetryPolicy

    transport = TransientQueryInterruptionTransport()
    monkeypatch.setattr(
        final_test,
        "UrllibOfficialQueryTransport",
        lambda: transport,
        raising=False,
    )
    monkeypatch.setattr(
        final_test,
        "RetryPolicy",
        lambda: RetryPolicy(
            attempts=3,
            timeout_seconds=0.1,
            minimum_interval_seconds=0,
        ),
    )

    final_test.main(["collect-queries", *prepared_cli.argv])

    assert transport.query_attempts == 5
    assert (
        prepared_cli.output_root
        / "official_query_coverage/official_query_coverage.json"
    ).is_file()


def test_supervise_queries_restarts_signal_exit_and_records_old_and_new_pid(
    prepared_cli: PreparedCli,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_multifactor.cli import final_test

    @dataclass
    class Process:
        pid: int
        returncode: int

        def wait(self) -> int:
            return self.returncode

    processes = iter(
        [
            Process(pid=401, returncode=-signal.SIGKILL),
            Process(pid=402, returncode=0),
        ]
    )
    commands: list[list[str]] = []

    def start(command: list[str], *, cwd: Path) -> Process:
        assert cwd == prepared_cli.attempt.code_root
        commands.append(command)
        return next(processes)

    monkeypatch.setattr(final_test, "_start_collector_process", start)
    attempts = prepared_cli.attempt.data_root / "processed/final_test/attempts"
    before = _tree_bytes(attempts)
    ledger_before = prepared_cli.attempt.consumption_ledger.read_bytes()

    final_test.main(["supervise-queries", *prepared_cli.argv])

    assert _tree_bytes(attempts) == before
    assert prepared_cli.attempt.consumption_ledger.read_bytes() == ledger_before
    assert len(commands) == 2
    assert all("collect-queries" in command for command in commands)
    assert all("resume" not in command and "prepare" not in command for command in commands)
    log = json.loads(
        (
            prepared_cli.attempt.data_root
            / "processed/final_test_runtime"
            / prepared_cli.attempt.attempt_id
            / "collection_process_recovery.json"
        ).read_bytes()
    )
    assert log["status"] == "complete"
    assert log["restart_count"] == 1
    assert log["events"][2]["predecessor_pid"] == 401
    assert log["events"][2]["pid"] == 402


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


def test_publish_review_rejects_foreign_workspace_after_authorization_preflight(
    prepared_cli: PreparedCli,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_multifactor.cli import final_test

    foreign = prepared_cli.attempt.data_root / "foreign-evidence"
    queue = (
        foreign
        / "official_document_workspace/review_sessions/session/review_queue.parquet"
    )
    monkeypatch.setattr(
        final_test,
        "publish_review_submission",
        lambda **_kwargs: pytest.fail("foreign review must not be published"),
    )
    argv = [
        "publish-review",
        *prepared_cli.argv[:-1],
        str(foreign),
        "--review-queue",
        str(queue),
        "--candidate-manifest",
        str(foreign / "candidate-manifest.json"),
        "--announcement-decisions",
        str(foreign / "announcement-decisions.parquet"),
        "--corporate-dispositions",
        str(foreign / "corporate-dispositions.parquet"),
        "--corporate-facts",
        str(foreign / "corporate-facts.parquet"),
        "--security-facts",
        str(foreign / "security-facts.parquet"),
        "--reviewer-id",
        "reviewer",
        "--reviewed-at",
        "2026-07-27T20:00:00+08:00",
    ]

    with pytest.raises(ValueError, match="output root differs from attempt"):
        final_test.main(argv)


def test_prepare_review_batches_cli_dispatches_bound_symbol_shards(
    prepared_cli: PreparedCli,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ashare_multifactor.cli import final_test
    from ashare_multifactor.cli import final_test_review

    queue = (
        prepared_cli.output_root
        / "official_document_workspace/review_sessions/session/review_queue.parquet"
    )
    candidate_manifest = prepared_cli.output_root / "candidate/manifest.json"
    manifest = prepared_cli.output_root / "batch-plan/review_batch_plan.json"
    captured: dict[str, object] = {}

    def prepare(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(manifest_path=manifest, batches=(object(), object()))

    monkeypatch.setattr(
        final_test_review,
        "prepare_review_batch_workspace",
        prepare,
    )

    final_test.main(
        [
            "prepare-review-batches",
            *prepared_cli.argv,
            "--review-queue",
            str(queue),
            "--candidate-manifest",
            str(candidate_manifest),
            "--symbols-per-batch",
            "50",
        ]
    )

    assert captured["candidate_manifest_path"] == candidate_manifest
    assert captured["symbols_per_batch"] == 50
    assert capsys.readouterr().out.splitlines() == [
        str(manifest),
        "batch_count=2",
    ]
