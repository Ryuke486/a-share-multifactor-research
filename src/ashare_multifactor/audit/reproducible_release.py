from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Callable, Mapping, Protocol
import uuid

from ashare_multifactor.audit.publication import publish_release


def _stage7_core_paths() -> tuple[str, ...]:
    from ashare_multifactor.validation.pipeline import CORE_VALIDATION_FILES

    return CORE_VALIDATION_FILES


def _stage8_core_paths() -> tuple[str, ...]:
    from ashare_multifactor.robustness.pipeline import CORE_ROBUSTNESS_FILES

    return CORE_ROBUSTNESS_FILES


@dataclass(frozen=True)
class _CompatibilityProfile:
    name: str
    run_root: Path
    certificate_path: Path
    core_paths_source: Callable[[], tuple[str, ...]]
    include_resource_usage: bool
    strict_tree: bool
    safe_run_id: bool
    incomplete_error_prefix: str
    code_drift_error: str
    input_drift_error: str
    output_difference_label: str
    verify_manifest_hashes: bool
    manifest_incomplete_error_prefix: str | None
    manifest_hash_error_prefix: str | None
    compare_inputs_first: bool
    different_code_error: str
    different_inputs_error: str
    distinct_runs_error: str | None
    full_pipeline_certificate: bool
    source_run_error: str
    source_code_error: str
    source_input_error: str
    source_hash_error_prefix: str

    @property
    def core_paths(self) -> tuple[str, ...]:
        return self.core_paths_source()


_STAGE7_V1 = _CompatibilityProfile(
    name="stage7_v1",
    run_root=Path("artifacts/validation_evaluation/full_runs"),
    certificate_path=Path(
        "processed/validation_evaluation/full_reproducibility.json"
    ),
    core_paths_source=_stage7_core_paths,
    include_resource_usage=True,
    strict_tree=True,
    safe_run_id=True,
    incomplete_error_prefix="validation run is incomplete: ",
    code_drift_error="code identity changed during validation run",
    input_drift_error="frozen inputs changed during validation run",
    output_difference_label="validation",
    verify_manifest_hashes=True,
    manifest_incomplete_error_prefix="validation run manifest is incomplete: ",
    manifest_hash_error_prefix="validation run manifest hash mismatch: ",
    compare_inputs_first=True,
    different_code_error="validation runs used different code identities",
    different_inputs_error="validation runs used different frozen inputs",
    distinct_runs_error="full validation reproducibility requires distinct runs",
    full_pipeline_certificate=True,
    source_run_error="certified validation source run identity changed",
    source_code_error="certified validation source code identity changed",
    source_input_error="certified validation source input identity changed",
    source_hash_error_prefix="validation run manifest hash mismatch: ",
)
_STAGE8_V1 = _CompatibilityProfile(
    name="stage8_v1",
    run_root=Path("artifacts/robustness/full_runs"),
    certificate_path=Path("processed/robustness/reproducibility.json"),
    core_paths_source=_stage8_core_paths,
    include_resource_usage=False,
    strict_tree=False,
    safe_run_id=False,
    incomplete_error_prefix="robustness run is incomplete: ",
    code_drift_error="code identity changed during robustness run",
    input_drift_error="robustness inputs changed during run",
    output_difference_label="robustness",
    verify_manifest_hashes=False,
    manifest_incomplete_error_prefix=None,
    manifest_hash_error_prefix=None,
    compare_inputs_first=False,
    different_code_error="robustness runs used different code identities",
    different_inputs_error="robustness runs used different inputs",
    distinct_runs_error=None,
    full_pipeline_certificate=False,
    source_run_error="reproducible source run identity changed",
    source_code_error="reproducible source code identity changed",
    source_input_error="reproducible source input identity changed",
    source_hash_error_prefix="reproducible source hash changed: ",
)
_PROFILES = {
    _STAGE7_V1.name: _STAGE7_V1,
    _STAGE8_V1.name: _STAGE8_V1,
}


@dataclass(frozen=True)
class _RunBinding:
    profile: str
    data_root: Path
    run_id: str
    code: Mapping[str, object]
    inputs: Mapping[str, object]


@dataclass(frozen=True)
class _ReleasePreparation:
    staged_root: Path
    publication_root: Path
    staged_datasets: Path
    staged_artifacts: Path
    lineage: Mapping[str, object]
    manifest_metadata: Mapping[str, object] | None = None
    publish_kwargs: Mapping[str, object] | None = None


class _Adapter(Protocol):
    def bind(self, code_root: Path, *, run_id: str) -> _RunBinding: ...

    def execute(
        self,
        binding: _RunBinding,
        run_root: Path,
    ) -> Mapping[str, object] | None: ...

    def prepare_release(
        self,
        binding: _RunBinding,
        source: Path,
        certificate: dict[str, object],
        *,
        run_id: str,
        options: object,
    ) -> _ReleasePreparation: ...


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def _validate_run_id(profile: _CompatibilityProfile, run_id: str) -> None:
    if profile.safe_run_id and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", run_id
    ) is None:
        raise ValueError("validation run ID must be a safe slug")


def _validate_compatibility_run_id(profile_name: str, run_id: str) -> str:
    _validate_run_id(_PROFILES[profile_name], run_id)
    return run_id


def _assert_clean_release_identity(identity: Mapping[str, object]) -> None:
    if identity.get("dirty") is not False:
        raise ValueError("current code identity is dirty")


def _validate_run_outputs(
    run_root: Path,
    profile: _CompatibilityProfile,
    *,
    require_manifest: bool = False,
) -> None:
    _validate_run_tree(
        run_root,
        profile,
        require_manifest=require_manifest,
    )
    missing = [
        relative
        for relative in profile.core_paths
        if not (run_root / relative).is_file()
    ]
    if missing:
        raise FileNotFoundError(
            profile.incomplete_error_prefix + ", ".join(missing)
        )


def _validate_run_tree(
    run_root: Path,
    profile: _CompatibilityProfile,
    *,
    require_manifest: bool,
) -> set[str]:
    if profile.strict_tree:
        if run_root.is_symlink():
            raise ValueError("validation run root must not be a symlink")
        if any(path.is_symlink() for path in run_root.rglob("*")):
            raise ValueError("validation run must not contain symlinks")
    actual = {
        path.relative_to(run_root).as_posix()
        for path in run_root.rglob("*")
        if path.is_file()
    }
    if profile.strict_tree:
        allowed = set(profile.core_paths)
        if require_manifest:
            allowed.add("run_manifest.json")
        unexpected = sorted(actual - allowed)
        if unexpected:
            raise ValueError(
                "validation run contains unexpected files: "
                + ", ".join(unexpected)
            )
    return actual


def _finalize_compatibility_run(
    profile_name: str,
    run_root: Path,
    *,
    run_id: str,
    code: Mapping[str, object],
    inputs: Mapping[str, object],
    validate_domain_outputs: Callable[[Path], None],
    resource_usage: Mapping[str, object] | None = None,
) -> dict[str, object]:
    profile = _PROFILES[profile_name]
    _validate_run_outputs(run_root, profile)
    validate_domain_outputs(run_root)
    return _write_run_manifest(
        profile,
        run_root,
        run_id=run_id,
        code=code,
        inputs=inputs,
        resource_usage=resource_usage,
    )


def _write_run_manifest(
    profile: _CompatibilityProfile,
    run_root: Path,
    *,
    run_id: str,
    code: Mapping[str, object],
    inputs: Mapping[str, object],
    resource_usage: Mapping[str, object] | None,
) -> dict[str, object]:
    files = [
        {
            "path": relative,
            "sha256": _sha256(run_root / relative),
            "size": (run_root / relative).stat().st_size,
        }
        for relative in profile.core_paths
    ]
    manifest: dict[str, object] = {
        "run_id": run_id,
        "code": dict(code),
        "inputs": dict(inputs),
        "core_file_count": len(files),
        "files": files,
    }
    if profile.include_resource_usage:
        if resource_usage is None:
            raise ValueError("Stage-7 resource usage is required")
        manifest["resource_usage"] = dict(resource_usage)
    (run_root / "run_manifest.json").write_bytes(_json_bytes(manifest))
    return manifest


def run_once(
    adapter: _Adapter,
    code_root: Path,
    *,
    run_id: str | None = None,
) -> Path:
    actual_run_id = run_id or uuid.uuid4().hex
    binding = adapter.bind(code_root.resolve(), run_id=actual_run_id)
    profile = _PROFILES[binding.profile]
    _validate_run_id(profile, actual_run_id)
    run_root = binding.data_root / profile.run_root / actual_run_id
    if run_root.exists():
        raise FileExistsError(run_root)
    run_root.mkdir(parents=True)
    try:
        execution = dict(adapter.execute(binding, run_root) or {})
        rebound = adapter.bind(code_root.resolve(), run_id=actual_run_id)
        if rebound.code != binding.code:
            raise ValueError(profile.code_drift_error)
        if rebound.inputs != binding.inputs:
            raise ValueError(profile.input_drift_error)
        _validate_run_outputs(run_root, profile)
        _write_run_manifest(
            profile,
            run_root,
            run_id=actual_run_id,
            code=binding.code,
            inputs=binding.inputs,
            resource_usage=execution.get("resource_usage"),
        )
    except BaseException:
        shutil.rmtree(run_root, ignore_errors=True)
        raise
    return run_root


def _read_manifest(run_root: Path) -> dict[str, object]:
    return json.loads((run_root / "run_manifest.json").read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_manifest(
    run_root: Path,
    profile: _CompatibilityProfile,
) -> dict[str, object]:
    manifest = _read_manifest(run_root)
    _validate_run_outputs(run_root, profile, require_manifest=True)
    if profile.verify_manifest_hashes:
        assert profile.manifest_incomplete_error_prefix is not None
        assert profile.manifest_hash_error_prefix is not None
        if manifest.get("core_file_count") != len(profile.core_paths):
            raise ValueError(
                profile.manifest_incomplete_error_prefix + run_root.name
            )
        recorded = {
            item["path"]: item["sha256"]
            for item in manifest.get("files", [])
        }
        for relative in profile.core_paths:
            if recorded.get(relative) != _sha256(run_root / relative):
                raise ValueError(
                    profile.manifest_hash_error_prefix
                    + f"{run_root.name}/{relative}"
                )
    return manifest


def _compare_core_outputs(
    profile: _CompatibilityProfile,
    first: Path,
    second: Path,
    relative_paths: tuple[str, ...] | None = None,
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    paths = profile.core_paths if relative_paths is None else relative_paths
    for relative in paths:
        left = first / relative
        right = second / relative
        if not left.is_file() or not right.is_file():
            raise FileNotFoundError(relative)
        first_hash = _sha256(left)
        second_hash = _sha256(right)
        if first_hash != second_hash:
            raise ValueError(
                f"{profile.output_difference_label} runs differ: {relative}"
            )
        hashes[relative] = second_hash
    return hashes


def _compare_run_manifests(
    profile: _CompatibilityProfile,
    roots: tuple[Path, Path],
) -> dict[str, object]:
    manifests = tuple(_verify_manifest(root, profile) for root in roots)
    comparisons = (
        (
            "inputs",
            profile.different_inputs_error,
        ),
        (
            "code",
            profile.different_code_error,
        ),
    )
    if not profile.compare_inputs_first:
        comparisons = tuple(reversed(comparisons))
    for field, error in comparisons:
        if manifests[0][field] != manifests[1][field]:
            raise ValueError(error)
    return {
        "outputs_identical": True,
        "core_file_count": len(profile.core_paths),
        "core_hashes": _compare_core_outputs(profile, *roots),
        "run_ids": [manifests[0]["run_id"], manifests[1]["run_id"]],
        "code": manifests[0]["code"],
        "inputs": manifests[0]["inputs"],
    }


def _certify_runs(
    profile: _CompatibilityProfile,
    data_root: Path,
    run_ids: tuple[str, str],
) -> dict[str, object]:
    if profile.distinct_runs_error is not None and run_ids[0] == run_ids[1]:
        raise ValueError(profile.distinct_runs_error)
    roots = tuple(data_root / profile.run_root / run_id for run_id in run_ids)
    comparison = _compare_run_manifests(profile, roots)
    code = comparison["code"]
    clean = isinstance(code, dict) and code.get("dirty") is False
    common: dict[str, object] = {
        "run_ids": comparison["run_ids"],
        "core_file_count": comparison["core_file_count"],
        "outputs_identical": True,
        "release_eligible": clean,
        "reason": None if clean else "git_identity_is_dirty",
        "code": code,
        "inputs": comparison["inputs"],
        "core_hashes": comparison["core_hashes"],
    }
    if profile.full_pipeline_certificate:
        result = {
            "scope": "full_pipeline",
            "run_ids": common["run_ids"],
            "core_file_count": common["core_file_count"],
            "outputs_identical": True,
            "full_pipeline_reproducible": clean,
            "release_eligible": common["release_eligible"],
            "reason": common["reason"],
            "code": common["code"],
            "inputs": common["inputs"],
            "core_hashes": common["core_hashes"],
            "run_manifest_sha256": [
                _sha256(root / "run_manifest.json") for root in roots
            ],
        }
    else:
        result = common
    destination = data_root / profile.certificate_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(_json_bytes(result))
    return result


def _certify_compatibility_runs(
    profile_name: str,
    data_root: Path,
    first_run_id: str,
    second_run_id: str,
) -> dict[str, object]:
    return _certify_runs(
        _PROFILES[profile_name],
        data_root,
        (first_run_id, second_run_id),
    )


def _compare_compatibility_runs(
    profile_name: str,
    first: Path,
    second: Path,
) -> dict[str, object]:
    return _compare_run_manifests(_PROFILES[profile_name], (first, second))


def _compare_compatibility_outputs(
    profile_name: str,
    first: Path,
    second: Path,
    relative_paths: tuple[str, ...],
) -> None:
    _compare_core_outputs(
        _PROFILES[profile_name],
        first,
        second,
        relative_paths,
    )


def reproduce(
    adapter: _Adapter,
    code_root: Path,
    *,
    run_id_prefix: str | None = None,
) -> dict[str, object]:
    prefix = run_id_prefix or uuid.uuid4().hex
    run_ids = (f"{prefix}_1", f"{prefix}_2")
    run_once(adapter, code_root, run_id=run_ids[0])
    second = run_once(adapter, code_root, run_id=run_ids[1])
    profile, data_root = _profile_and_data_root(second)
    return _certify_runs(
        profile,
        data_root,
        run_ids,
    )


def _profile_and_data_root(
    run_root: Path,
) -> tuple[_CompatibilityProfile, Path]:
    for profile in _PROFILES.values():
        parts = profile.run_root.parts
        if run_root.parent.parts[-len(parts) :] == parts:
            data_root = run_root
            for _ in range(len(parts) + 1):
                data_root = data_root.parent
            return profile, data_root
    raise ValueError(f"unknown reproducibility run root: {run_root}")


def _certificate_run_ids(certificate: Mapping[str, object]) -> tuple[str, str]:
    values = certificate.get("run_ids")
    if (
        not isinstance(values, list)
        or len(values) != 2
        or not all(isinstance(item, str) for item in values)
    ):
        raise ValueError("reproducibility certificate does not bind two runs")
    return values[0], values[1]


def _verify_certified_source(
    profile: _CompatibilityProfile,
    data_root: Path,
    certificate: Mapping[str, object],
) -> Path:
    run_id = _certificate_run_ids(certificate)[1]
    source = data_root / profile.run_root / run_id
    _verify_compatibility_source(profile.name, source, certificate)
    return source


def _verify_compatibility_source(
    profile_name: str,
    source: Path,
    certificate: Mapping[str, object],
) -> None:
    profile = _PROFILES[profile_name]
    run_id = _certificate_run_ids(certificate)[1]
    manifest = _read_manifest(source)
    if source.name != run_id:
        raise ValueError(profile.source_run_error)
    if manifest.get("run_id") != run_id:
        raise ValueError(profile.source_run_error)
    if manifest.get("code") != certificate.get("code"):
        raise ValueError(profile.source_code_error)
    if manifest.get("inputs") != certificate.get("inputs"):
        raise ValueError(profile.source_input_error)
    manifest_hashes = {
        item["path"]: item["sha256"]
        for item in manifest.get("files", [])
    }
    expected = certificate.get("core_hashes", {})
    for relative in profile.core_paths:
        actual = _sha256(source / relative)
        if (
            not isinstance(expected, dict)
            or actual != expected.get(relative)
            or actual != manifest_hashes.get(relative)
        ):
            raise ValueError(
                profile.source_hash_error_prefix
                + (
                    relative
                    if not profile.verify_manifest_hashes
                    else f"{run_id}/{relative}"
                )
            )


def release(
    adapter: _Adapter,
    code_root: Path,
    *,
    run_id: str,
    publish: bool,
    options: object = None,
    certificate: Mapping[str, object] | None = None,
) -> object:
    code_root = code_root.resolve()
    binding = adapter.bind(code_root, run_id=run_id)
    profile = _PROFILES[binding.profile]
    _validate_run_id(profile, run_id)
    if publish:
        _assert_clean_release_identity(binding.code)
    recorded = (
        dict(certificate)
        if certificate is not None
        else json.loads(
            (binding.data_root / profile.certificate_path).read_text(
                encoding="utf-8"
            )
        )
    )
    rebuilt = _certify_runs(
        profile,
        binding.data_root,
        _certificate_run_ids(recorded),
    )
    if publish and binding.code != rebuilt.get("code"):
        raise ValueError("current code identity differs from certified runs")
    if binding.inputs != rebuilt.get("inputs"):
        raise ValueError("current inputs differ from certified runs")
    if publish and rebuilt.get("release_eligible") is not True:
        raise ValueError("reproducibility gate is not release eligible")
    source = _verify_certified_source(profile, binding.data_root, rebuilt)
    prepared = adapter.prepare_release(
        binding,
        source,
        rebuilt,
        run_id=run_id,
        options=options,
    )
    rebound = adapter.bind(code_root, run_id=run_id)
    if publish and rebound.code != binding.code:
        raise ValueError("code identity changed during release preparation")
    if rebound.inputs != binding.inputs:
        raise ValueError("inputs changed during release preparation")
    _verify_certified_source(profile, binding.data_root, rebuilt)
    if not publish:
        return prepared
    return publish_release(
        prepared.publication_root,
        run_id=run_id,
        staged_datasets=prepared.staged_datasets,
        staged_artifacts=prepared.staged_artifacts,
        lineage=dict(prepared.lineage),
        manifest_metadata=(
            dict(prepared.manifest_metadata)
            if prepared.manifest_metadata is not None
            else None
        ),
        **dict(prepared.publish_kwargs or {}),
    )


__all__ = ("run_once", "reproduce", "release")
