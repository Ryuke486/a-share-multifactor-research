"""Regression tests for descriptor-only final-root lifecycle branches."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import shutil

import polars as pl
import pytest

from test_build import _config, _write_pair
from test_final_test_data import FINAL_END, FINAL_START, _authorized_context, _build

from ashare_multifactor.final_test import data_extension, preparation
from ashare_multifactor.final_test.backtest import (
    FinalTestSignals,
    run_final_test_backtest,
)
from ashare_multifactor.final_test.data_publication import (
    resolve_final_test_data_panel,
)
from ashare_multifactor.final_test.final_root_binding import FinalRootBinding
from ashare_multifactor.final_test.panel_binding import bind_panel_snapshot
from ashare_multifactor.final_test.registry import append_attempt_state
from ashare_multifactor.final_test.signals import build_final_test_signals


def _published_panel(tmp_path: Path):
    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    _build(config, authorization, code_root)
    return config, code_root, authorization, config.paths.processed / "final_test"


def test_bound_panel_snapshot_validates_held_panel_after_root_replacement(
    tmp_path: Path,
) -> None:
    """A later same-name root must not supply the snapshot validator's files."""
    _config_value, _code_root, _authorization, final_root = _published_panel(tmp_path)
    displaced = tmp_path / "final-root-a"

    with FinalRootBinding.open(final_root) as binding:
        final_root.rename(displaced)
        shutil.copytree(displaced, final_root)
        (final_root / "daily_panel/manifest.json").write_text(
            "{}\n", encoding="utf-8"
        )
        replacement_before = {
            path.relative_to(final_root).as_posix(): path.read_bytes()
            for path in final_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        snapshot = bind_panel_snapshot(
            final_root / "daily_panel",
            datasets_fd=binding.final_fd,
            expected_manifest_sha256=resolve_final_test_data_panel(
                displaced
            ).data_manifest_sha256,
            period=_config_value.test,
        )
        try:
            assert snapshot.source is not None
            assert snapshot.source.files
            replacement_after = {
                path.relative_to(final_root).as_posix(): path.read_bytes()
                for path in final_root.rglob("*")
                if path.is_file() and not path.is_symlink()
            }
            assert replacement_after == replacement_before
        finally:
            snapshot.close()
            shutil.rmtree(final_root)
            displaced.rename(final_root)


def test_bound_preparation_completion_never_uses_named_panel_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound completion path must use only final-root descriptors."""
    config, code_root, authorization, final_root = _published_panel(tmp_path)
    append_attempt_state(
        final_root / "attempts", attempt_id=authorization.attempt_id, state="preparing"
    )
    monkeypatch.setattr(preparation, "_load_frozen_config", lambda *_args: config)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("bound preparation reopened final_test by path")

    monkeypatch.setattr(preparation, "resolve_final_test_data_panel", forbidden)
    monkeypatch.setattr(preparation, "validate_panel_source", forbidden)
    monkeypatch.setattr(data_extension, "resolve_final_test_data_panel", forbidden)

    with FinalRootBinding.open(final_root) as binding:
        result = preparation._complete_preparation(
            final_root,
            authorization=authorization,
            code_root=code_root,
            data_root=tmp_path,
            root_binding=binding,
        )

    assert result.symbol_count == 1


def test_bound_preparation_recovery_never_uses_named_panel_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prior preparation resumes from the held root, not a re-opened name."""
    _config_value, code_root, authorization, final_root = _published_panel(tmp_path)
    preparation._publish_preparation(
        final_root,
        authorization=authorization,
        resolution=resolve_final_test_data_panel(final_root),
        symbols=["000001"],
    )

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("bound preparation recovery reopened final_test by path")

    monkeypatch.setattr(preparation, "resolve_final_test_data_panel", forbidden)

    with FinalRootBinding.open(final_root) as binding:
        result = preparation._recover_preparation(
            final_root,
            registry_root=final_root / "attempts",
            authorization=authorization,
            code_root=code_root,
            data_root=tmp_path,
            root_binding=binding,
        )

    assert result.symbol_count == 1


def test_bound_published_stage2_recovery_never_uses_named_panel_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Published Stage-2 recovery must resolve the held final root only."""
    config, code_root, authorization, final_root = _published_panel(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("bound Stage-2 recovery reopened final_test by path")

    monkeypatch.setattr(data_extension, "resolve_final_test_data_panel", forbidden)

    with FinalRootBinding.open(final_root) as binding:
        resolution = data_extension.recover_final_test_daily_panel(
            config,
            authorization,
            FINAL_START,
            FINAL_END,
            code_root=code_root,
            root_binding=binding,
        )

    assert resolution.claim_status == "published"


def test_bound_stage2_build_publishes_through_held_descriptor_chain(
    tmp_path: Path,
) -> None:
    """A normal bound Stage-2 build must never need a final-root path reopen."""
    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    final_root = config.paths.processed / "final_test"

    with FinalRootBinding.open(final_root) as binding:
        manifest = data_extension.build_final_test_daily_panel(
            config,
            authorization,
            FINAL_START,
            FINAL_END,
            code_root=code_root,
            root_binding=binding,
        )

    assert manifest.rows == 1
    assert (final_root / "daily_panel/data_manifest.json").is_file()


def test_bound_publishing_stage2_recovery_uses_held_stage2_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-rename Stage-2 recovery reuses descriptors for its staging tree."""
    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    final_root = config.paths.processed / "final_test"
    original_update = data_extension._update_claim

    def interrupt_published(*args: object, status: str, **kwargs: object) -> None:
        if status == "published":
            raise KeyboardInterrupt("inject post-rename recovery state")
        original_update(*args, status=status, **kwargs)

    monkeypatch.setattr(data_extension, "_update_claim", interrupt_published)
    with FinalRootBinding.open(final_root) as binding:
        with pytest.raises(KeyboardInterrupt, match="post-rename"):
            data_extension.build_final_test_daily_panel(
                config,
                authorization,
                FINAL_START,
                FINAL_END,
                code_root=code_root,
                root_binding=binding,
            )
    monkeypatch.setattr(data_extension, "_update_claim", original_update)

    with FinalRootBinding.open(final_root) as binding:
        resolution = data_extension.recover_final_test_daily_panel(
            config,
            authorization,
            FINAL_START,
            FINAL_END,
            code_root=code_root,
            root_binding=binding,
        )

    assert resolution.claim_status == "published"


def test_bound_stage2_recovery_rejects_root_replacement_without_touching_b(
    tmp_path: Path,
) -> None:
    """A namespace switch is fail-closed before the replacement can be used."""
    config, code_root, authorization, final_root = _published_panel(tmp_path)
    displaced = tmp_path / "final-root-a"
    original_before = {
        path.relative_to(final_root).as_posix(): path.read_bytes()
        for path in final_root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }

    with FinalRootBinding.open(final_root) as binding:
        final_root.rename(displaced)
        shutil.copytree(displaced, final_root)
        replacement_before = {
            path.relative_to(final_root).as_posix(): path.read_bytes()
            for path in final_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        with pytest.raises(ValueError, match="claimed final-test root namespace identity changed"):
            data_extension.recover_final_test_daily_panel(
                config,
                authorization,
                FINAL_START,
                FINAL_END,
                code_root=code_root,
                root_binding=binding,
            )
        replacement_after = {
            path.relative_to(final_root).as_posix(): path.read_bytes()
            for path in final_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        assert replacement_after == replacement_before
        assert {
            path.relative_to(displaced).as_posix(): path.read_bytes()
            for path in displaced.rglob("*")
            if path.is_file() and not path.is_symlink()
        } == original_before
        shutil.rmtree(final_root)
        displaced.rename(final_root)


@pytest.mark.parametrize("entry", ["signals", "backtest"])
def test_bound_signal_and_backtest_entries_do_not_resolve_final_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
) -> None:
    """Execution entries must treat a binding as final-root authority."""
    config, code_root, authorization, final_root = _published_panel(tmp_path)
    import ashare_multifactor.final_test.backtest as backtest_module
    import ashare_multifactor.final_test.signals as signals_module

    original_resolve = Path.resolve

    def guarded_resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if path == final_root:
            pytest.fail("bound execution resolved final_test by name")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    monkeypatch.setattr(signals_module, "_load_frozen_config", lambda *_args: config)
    monkeypatch.setattr(backtest_module, "_load_frozen_config", lambda *_args: config)
    monkeypatch.setattr(signals_module, "_verify_data_authorization", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backtest_module, "_verify_data_authorization", lambda *_args, **_kwargs: None)

    with FinalRootBinding.open(final_root) as binding:
        if entry == "signals":
            with pytest.raises(ValueError, match="frozen factor and portfolio"):
                build_final_test_signals(
                    authorization,
                    code_root=code_root,
                    final_root=final_root,
                    root_binding=binding,
                )
        else:
            empty = FinalTestSignals(
                factor_panel=pl.DataFrame(),
                composite_scores=pl.DataFrame(),
                composite_weights=pl.DataFrame(),
                target_weights=pl.DataFrame(),
            )
            with pytest.raises(ValueError, match="Task 3 final targets"):
                run_final_test_backtest(
                    authorization,
                    empty,
                    code_root=code_root,
                    final_root=final_root,
                    root_binding=binding,
                )
