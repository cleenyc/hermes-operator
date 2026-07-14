"""Build the complete, reproducible Hermes Operator release archive."""

from __future__ import annotations

import gzip
import os
from pathlib import Path
import tarfile


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.4.0"
PLUGIN_VERSION = "1.5.0"
ARCHIVE_ROOT = f"hermes-operator-{VERSION}"
OUTPUT = ROOT / "dist" / f"hermes-operator-{VERSION}-complete.tar.gz"
EXPECTED_WHEELS = (
    f"hermes_operator-{VERSION}-py3-none-any.whl",
    f"hermes_operator_plugin-{PLUGIN_VERSION}-py3-none-any.whl",
)
INCLUDE = (
    "README.md",
    "AUTHORS.md",
    "CHANGELOG.md",
    "LICENSE",
    "Makefile",
    "pyproject.toml",
    "Dockerfile",
    "compose.yaml",
    ".github",
    "config",
    "deploy",
    "docs",
    "integrations",
    "scripts",
    "src",
    "tests",
)
EXCLUDED_PARTS = {"__pycache__", "build", ".pytest_cache", ".mypy_cache"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}


def _files() -> list[Path]:
    files: list[Path] = []
    for name in INCLUDE:
        path = ROOT / name
        if path.is_file():
            files.append(path)
            continue
        files.extend(candidate for candidate in path.rglob("*") if candidate.is_file())
    files.extend(
        path
        for name in EXPECTED_WHEELS
        if (path := ROOT / "dist" / name).is_file()
    )
    return sorted(
        {
            path
            for path in files
            if not EXCLUDED_PARTS.intersection(path.parts)
            and path.suffix not in EXCLUDED_SUFFIXES
            and ".egg-info" not in path.as_posix()
        },
        key=lambda path: path.relative_to(ROOT).as_posix(),
    )


def _normalized(info: tarfile.TarInfo, epoch: int) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = epoch
    info.mode = 0o755 if info.mode & 0o111 else 0o644
    return info


def main() -> int:
    # 1980-01-01 is accepted by both tar and ZIP-based wheel tooling.
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "315532800"))
    missing_wheels = [
        name for name in EXPECTED_WHEELS if not (ROOT / "dist" / name).is_file()
    ]
    if missing_wheels:
        raise RuntimeError(
            "release archive requires exact-version wheels: "
            + ", ".join(missing_wheels)
        )
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(OUTPUT.suffix + ".tmp")
    with temporary.open("wb") as raw:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw,
            mtime=epoch,
        ) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for path in _files():
                    relative = path.relative_to(ROOT)
                    archive.add(
                        path,
                        arcname=(Path(ARCHIVE_ROOT) / relative).as_posix(),
                        recursive=False,
                        filter=lambda info: _normalized(info, epoch),
                    )
    temporary.replace(OUTPUT)
    with tarfile.open(OUTPUT, "r:gz") as archive:
        members = archive.getmembers()
        if not members or any(
            not member.name.startswith(f"{ARCHIVE_ROOT}/") for member in members
        ):
            raise RuntimeError("release archive failed structural verification")
        names = {member.name for member in members}
        required = {
            f"{ARCHIVE_ROOT}/AUTHORS.md",
            f"{ARCHIVE_ROOT}/CHANGELOG.md",
            *(f"{ARCHIVE_ROOT}/dist/{name}" for name in EXPECTED_WHEELS),
        }
        if not required <= names:
            raise RuntimeError(
                "release archive is incomplete: "
                + ", ".join(sorted(required - names))
            )
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
