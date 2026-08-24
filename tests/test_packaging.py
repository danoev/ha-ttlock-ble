"""The manifest pin and the tested pin must name the same SDK release."""

from __future__ import annotations

import json
import subprocess
import tomllib
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
from zipfile import ZipFile

import pytest
from packaging.requirements import Requirement

_ROOT = Path(__file__).resolve().parent.parent
_SDK = "ttlock-ble"
_HACS_ARCHIVE = "ttlock_ble.zip"
_INTEGRATION_TREE = "HEAD:custom_components/ttlock_ble"


def _pinned(requirements: list[str], package: str) -> str | None:
    """Return an exact version or immutable direct URL for ``package``."""
    for raw_requirement in requirements:
        requirement = Requirement(raw_requirement)
        if requirement.name != package:
            continue
        if requirement.url is not None:
            return requirement.url
        specifier = str(requirement.specifier)
        if specifier.startswith("=="):
            return specifier.removeprefix("==")
    return None


def _assert_immutable_sdk_url(url: str) -> None:
    """Require the maintained SDK's immutable GitHub archive URL."""
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "github.com"
    assert parsed.hostname == "github.com"
    assert parsed.username is None
    assert parsed.password is None
    assert parsed.port is None
    assert parsed.params == ""
    assert parsed.query == ""
    assert parsed.fragment == ""

    archive_prefix = "/danoev/ttlock-ble/archive/"
    archive_suffix = ".tar.gz"
    assert parsed.path.startswith(archive_prefix)
    assert parsed.path.endswith(archive_suffix)
    archive_ref = parsed.path.removeprefix(archive_prefix).removesuffix(
        archive_suffix,
    )
    assert "/" not in archive_ref
    assert len(archive_ref) == 40
    assert all(character in "0123456789abcdef" for character in archive_ref)


def test_manifest_and_dev_group_pin_the_same_sdk_version() -> None:
    """Home Assistant installs the manifest pin; the suite runs the dev-group one.

    When they drift, every test passes against a release the running
    integration never sees.
    """
    manifest = json.loads(
        (_ROOT / "custom_components" / "ttlock_ble" / "manifest.json").read_text(),
    )
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())

    manifest_pin = _pinned(manifest["requirements"], _SDK)
    dev_pin = _pinned(pyproject["dependency-groups"]["dev"], _SDK)

    assert manifest_pin is not None, f"{_SDK} is not pinned in manifest.json"
    assert dev_pin is not None, f"{_SDK} is not pinned in the dev dependency group"
    assert manifest_pin == dev_pin
    _assert_immutable_sdk_url(manifest_pin)


@pytest.mark.parametrize(
    "url",
    [
        (
            "https://example.invalid/github.com/danoev/ttlock-ble/archive/"
            "53c78183e3ab7c1e96d6f9912a983b515388546c.tar.gz"
        ),
        (
            "https://github.com.example.invalid/danoev/ttlock-ble/archive/"
            "53c78183e3ab7c1e96d6f9912a983b515388546c.tar.gz"
        ),
        (
            "https://github.com/other/ttlock-ble/archive/"
            "53c78183e3ab7c1e96d6f9912a983b515388546c.tar.gz"
        ),
        "https://github.com/danoev/ttlock-ble/archive/not-a-commit.tar.gz",
    ],
)
def test_immutable_sdk_url_rejects_misleading_or_mutable_urls(url: str) -> None:
    """A GitHub-looking substring must not bypass structural validation."""
    with pytest.raises(AssertionError):
        _assert_immutable_sdk_url(url)


def test_hacs_zip_metadata_matches_fork_owned_release_workflow() -> None:
    """HACS and the release workflow must agree on the exact install asset."""
    hacs = json.loads((_ROOT / "hacs.json").read_text())
    workflow = (_ROOT / ".github" / "workflows" / "publish-hacs-zip.yml").read_text()

    assert hacs["zip_release"] is True
    assert hacs["filename"] == _HACS_ARCHIVE
    assert "release:\n    types: [published]" in workflow
    assert f"HEAD:custom_components/{_HACS_ARCHIVE.removesuffix('.zip')}" in workflow
    assert f'gh release upload "$TAG" "$RUNNER_TEMP/{_HACS_ARCHIVE}"' in workflow
    assert "roquerodrigo" not in workflow
    assert "secrets." not in workflow


def test_hacs_install_zip_contains_only_integration_files_at_root() -> None:
    """The release archive must extract directly into the HACS domain folder."""
    archive = subprocess.run(  # noqa: S603
        ["git", "archive", "--format=zip", _INTEGRATION_TREE],  # noqa: S607
        cwd=_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    tracked = subprocess.run(  # noqa: S603
        ["git", "ls-tree", "-r", "--name-only", _INTEGRATION_TREE],  # noqa: S607
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    with ZipFile(BytesIO(archive)) as install_zip:
        archived_files = {
            name for name in install_zip.namelist() if not name.endswith("/")
        }

    assert "manifest.json" in archived_files
    assert archived_files == set(tracked)
    assert not any(
        name.startswith(("custom_components/", "ttlock_ble/"))
        for name in archived_files
    )
