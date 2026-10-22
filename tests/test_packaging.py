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
from awesomeversion import AwesomeVersion
from packaging.requirements import Requirement
from packaging.version import Version

from scripts.validate_release import validate_release

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


def test_stable_identity_version_and_hacs_floor_remain_compatible() -> None:
    hacs = json.loads((_ROOT / "hacs.json").read_text())
    assert hacs == {
        "name": "TTLock BLE (danoev)",
        "zip_release": True,
        "filename": "ttlock_ble.zip",
        "homeassistant": "2026.5.1",
        "hacs": "2.0.5",
    }
    manifest = json.loads(
        (_ROOT / "custom_components/ttlock_ble/manifest.json").read_text()
    )
    assert manifest["domain"] == "ttlock_ble"
    assert manifest["name"] == "TTLock BLE"
    assert Version(manifest["version"]) > Version("3.5.1rc16")
    assert AwesomeVersion(manifest["version"]) > AwesomeVersion("3.5.1rc16")
    assert validate_release(_ROOT, "v3.5.2rc1") == "3.5.2rc1"


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
        assert install_zip.read("LICENSE") == (_ROOT / "LICENSE").read_bytes()

    assert "manifest.json" in archived_files
    assert archived_files == set(tracked)
    assert not any(
        name.startswith(("custom_components/", "ttlock_ble/"))
        for name in archived_files
    )


@pytest.fixture
def release_tree(tmp_path):
    component = tmp_path / "custom_components/ttlock_ble"
    component.mkdir(parents=True)
    (component / "manifest.json").write_text('{"version": "3.5.1rc12"}')
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "ha-ttlock-ble"\nversion = "3.5.1rc12"\n',
    )
    (tmp_path / ".release-please-manifest.json").write_text('{".": "3.5.1rc12"}')
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "ha-ttlock-ble"\nversion = "3.5.1rc12"\n',
    )
    return tmp_path


def _release_zip(root, version="3.5.1rc12", prefix=""):
    archive = root / _HACS_ARCHIVE
    with ZipFile(archive, "w") as install_zip:
        install_zip.writestr(f"{prefix}manifest.json", json.dumps({"version": version}))
    return archive


def test_release_versions_accept_consistent_source_and_zip(release_tree):
    assert (
        validate_release(
            release_tree,
            "v3.5.1rc12",
            _release_zip(release_tree),
        )
        == "3.5.1rc12"
    )


@pytest.mark.parametrize(
    "metadata",
    [
        "custom_components/ttlock_ble/manifest.json",
        "pyproject.toml",
        ".release-please-manifest.json",
        "uv.lock",
    ],
)
def test_release_versions_reject_any_stale_metadata(release_tree, metadata):
    path = release_tree / metadata
    path.write_text(path.read_text().replace("3.5.1rc12", "3.5.1rc10"))
    with pytest.raises(ValueError, match="Release version mismatch"):
        validate_release(release_tree, "v3.5.1rc12", _release_zip(release_tree))


def test_release_versions_reject_stale_zip_manifest(release_tree):
    with pytest.raises(ValueError, match=r"ZIP manifest=3\.5\.1rc10"):
        validate_release(
            release_tree,
            "v3.5.1rc12",
            _release_zip(release_tree, "3.5.1rc10"),
        )


def test_release_versions_reject_wrong_tag_and_missing_prefix(release_tree):
    with pytest.raises(ValueError, match="Release version mismatch"):
        validate_release(release_tree, "v3.5.1rc11")
    with pytest.raises(ValueError, match="Release tag must"):
        validate_release(release_tree, "3.5.1rc12")


def test_release_versions_reject_rc11_packaging_defect(release_tree):
    for path in release_tree.rglob("*"):
        if path.is_file():
            path.write_text(path.read_text().replace("3.5.1rc12", "3.5.1rc10"))
    with pytest.raises(ValueError, match="Release version mismatch"):
        validate_release(
            release_tree,
            "v3.5.1rc11",
            _release_zip(release_tree, "3.5.1rc10"),
        )


def test_release_versions_reject_nested_install_zip(release_tree):
    with pytest.raises(ValueError, match="Install ZIP must"):
        validate_release(
            release_tree,
            "v3.5.1rc12",
            _release_zip(release_tree, prefix="ttlock_ble/"),
        )


def test_release_workflow_checks_versions_before_packaging_and_upload():
    workflow = (_ROOT / ".github/workflows/publish-hacs-zip.yml").read_text()
    source_check = workflow.index(
        'run: python scripts/validate_release.py --tag "$TAG"'
    )
    package = workflow.index("git archive --format=zip")
    zip_check = workflow.index('--tag "$TAG" --archive "$ARCHIVE"')
    upload = workflow.index('gh release upload "$TAG"')
    assert source_check < package < zip_check < upload


def test_release_validation_cli_fails_before_upload_for_stale_versions(release_tree):
    result = subprocess.run(  # noqa: S603
        [
            str(_ROOT / ".venv/bin/python"),
            str(_ROOT / "scripts/validate_release.py"),
            "--root",
            str(release_tree),
            "--tag",
            "v3.5.1rc11",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "Release version mismatch" in result.stderr
