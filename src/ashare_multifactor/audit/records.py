from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path


_HEX_DIGITS = "0123456789abcdef"


@dataclass(frozen=True)
class FileRecord:
    path: str
    role: str
    sha256: str
    size_bytes: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(character in _HEX_DIGITS for character in value)


def is_git_oid_hex(value: str) -> bool:
    return len(value) == 40 and all(character in _HEX_DIGITS for character in value)


def write_json(path: Path, payload: object) -> None:
    """Write deterministic JSON in place; callers publish the enclosing tree atomically."""
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def file_record(path: Path, *, root: Path, role: str) -> FileRecord:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    try:
        relative = resolved_path.relative_to(resolved_root).as_posix()
    except ValueError as error:
        raise ValueError("recorded file must remain inside its declared root") from error
    return FileRecord(relative, role, sha256_file(resolved_path), resolved_path.stat().st_size)


def verify_file_record(record: FileRecord | dict[str, object], *, root: Path) -> Path:
    item = record if isinstance(record, FileRecord) else FileRecord(**record)
    path = (root / item.path).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError("recorded file escapes its declared root") from error
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size != item.size_bytes:
        raise ValueError(f"size mismatch for {item.path}")
    if sha256_file(path) != item.sha256:
        raise ValueError(f"digest mismatch for {item.path}")
    return path
