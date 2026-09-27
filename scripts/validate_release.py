"""Reject inconsistent source/release versions before attaching a HACS ZIP."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path
from zipfile import ZipFile


def validate_release(root: Path, tag: str, archive: Path | None = None) -> str:
    """Check every authoritative version and optionally the install archive."""
    if not tag.startswith("v") or not tag.removeprefix("v"):
        msg = "Release tag must be v followed by the project version"
        raise ValueError(msg)
    manifest = json.loads(
        (root / "custom_components/ttlock_ble/manifest.json").read_text(),
    )
    project = tomllib.loads((root / "pyproject.toml").read_text())
    release = json.loads((root / ".release-please-manifest.json").read_text())
    lock = tomllib.loads((root / "uv.lock").read_text())
    versions = {
        "tag": tag.removeprefix("v"),
        "source manifest": manifest["version"],
        "project": project["project"]["version"],
        "release metadata": release["."],
        "lock project": next(
            package["version"]
            for package in lock["package"]
            if package["name"] == project["project"]["name"]
        ),
    }
    if archive is not None:
        with ZipFile(archive) as install_zip:
            files = {name for name in install_zip.namelist() if not name.endswith("/")}
            if "manifest.json" not in files or any(
                name.startswith(("custom_components/", "ttlock_ble/")) for name in files
            ):
                msg = "Install ZIP must contain integration files at its root"
                raise ValueError(msg)
            versions["ZIP manifest"] = json.loads(
                install_zip.read("manifest.json"),
            )["version"]
    if len(set(versions.values())) != 1:
        msg = "Release version mismatch: " + ", ".join(
            f"{source}={version}" for source, version in versions.items()
        )
        raise ValueError(msg)
    return versions["tag"]


def main() -> None:
    """Run the same source/archive check used by the release workflow."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parent.parent
    )
    args = parser.parse_args()
    try:
        version = validate_release(args.root, args.tag, args.archive)
    except (ValueError, KeyError, StopIteration) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Release versions agree: {version}")  # noqa: T201 -- validation CLI


if __name__ == "__main__":
    main()
