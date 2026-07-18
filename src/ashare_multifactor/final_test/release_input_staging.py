"""Stage immutable panel and execution inputs for a final-test release."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ashare_multifactor.audit.secure_tree import (
    read_frozen_tree_at,
    write_frozen_tree_at,
)
from ashare_multifactor.final_test.execution_binding import BoundExecutionInputs
from ashare_multifactor.final_test.panel_binding import FrozenPanelSnapshot
from ashare_multifactor.final_test.recovery_secure_fs import (
    opened_directory,
    read_bytes_at,
)


def copy_release_inputs(
    *,
    panel_root: Path,
    panel_snapshot: FrozenPanelSnapshot | None = None,
    execution_inputs: BoundExecutionInputs,
    panel_manifest_sha256: str,
    panel_already_staged: bool = False,
    datasets: Path,
    artifacts: Path,
    datasets_fd: int | None = None,
    artifacts_fd: int | None = None,
) -> None:
    if not execution_inputs.files:
        raise ValueError("frozen execution inputs are empty")
    panel_files: dict[str, bytes] | None = None
    if panel_snapshot is not None:
        panel_snapshot.assert_bound()
        panel_files = panel_snapshot.read_frozen_files()
        manifest_bytes = panel_files.get("data_manifest.json")
    else:
        with opened_directory(panel_root, label="final daily panel") as panel_fd:
            if panel_already_staged:
                manifest_bytes = read_bytes_at(
                    panel_fd,
                    "data_manifest.json",
                    label="final daily panel manifest",
                )
            else:
                panel_files = read_frozen_tree_at(panel_fd, label="final daily panel")
                manifest_bytes = panel_files.get("data_manifest.json")
    if (
        manifest_bytes is None
        or hashlib.sha256(manifest_bytes).hexdigest() != panel_manifest_sha256
    ):
        raise ValueError("final daily panel manifest identity differs")
    if datasets_fd is not None and artifacts_fd is not None:
        write_frozen_tree_at(
            artifacts_fd,
            "execution_inputs",
            execution_inputs.files,
            resumable=False,
            label="staged frozen execution inputs",
        )
        if not panel_already_staged or (
            panel_snapshot is not None and panel_snapshot.detached
        ):
            if panel_files is None:
                raise RuntimeError("final daily panel bytes are missing")
            write_frozen_tree_at(
                datasets_fd,
                "final_daily_panel",
                panel_files,
                resumable=False,
                label="staged final daily panel",
            )
        return
    if datasets_fd is not None or artifacts_fd is not None:
        raise ValueError("both attempt staging descriptors are required")
    with opened_directory(datasets, label="attempt datasets staging") as datasets_fd:
        with opened_directory(
            artifacts, label="attempt artifacts staging"
        ) as artifacts_fd:
            if panel_files is None:
                raise RuntimeError("final daily panel bytes are missing")
            write_frozen_tree_at(
                artifacts_fd,
                "execution_inputs",
                execution_inputs.files,
                resumable=False,
                label="staged frozen execution inputs",
            )
            write_frozen_tree_at(
                datasets_fd,
                "final_daily_panel",
                panel_files,
                resumable=False,
                label="staged final daily panel",
            )
