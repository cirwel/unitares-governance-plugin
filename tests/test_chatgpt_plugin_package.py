from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "dev" / "build-chatgpt-plugin.py"


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BUILDER), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=check,
    )


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_hook_free(package: Path) -> None:
    manifest = _load(package / "plugin.json")
    extension = manifest["extensions"]["com.openai"]
    assert "hooks" not in extension
    assert not (package / "hooks").exists()
    assert not (package / "scripts").exists()


def _assert_skills_copied(package: Path) -> None:
    expected = {
        path.parent.name
        for path in (ROOT / "skills").glob("*/SKILL.md")
    } | {"setup-unitares"}
    actual = {path.parent.name for path in (package / "skills").glob("*/SKILL.md")}
    assert actual == expected
    assert not (package / "skills" / ".attestations").exists()


def test_builds_registered_chatgpt_app_reference(tmp_path: Path):
    package = tmp_path / "plugin"
    archive = tmp_path / "plugin.zip"

    result = _run(
        "--app-id",
        "plugin_asdk_app_example123",
        "--output",
        str(package),
        "--archive",
        str(archive),
    )

    assert "built ChatGPT plugin package" in result.stdout
    manifest = _load(package / "plugin.json")
    assert manifest["$schema"].endswith("/plugin.schema.json")
    assert manifest["extensions"]["com.openai"]["apps"] == "./.app.json"
    assert manifest["extensions"]["com.openai"]["onboardingSkill"] == (
        "./skills/setup-unitares/SKILL.md"
    )
    assert _load(package / ".app.json") == {
        "apps": {
            "unitares-governance": {
                "id": "plugin_asdk_app_example123",
                "required": True,
            }
        }
    }
    assert not (package / "mcp.json").exists()
    _assert_hook_free(package)
    _assert_skills_copied(package)

    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
    assert "plugin.json" in names
    assert ".app.json" in names
    assert "skills/setup-unitares/SKILL.md" in names
    assert all(not name.startswith("hooks/") for name in names)


def test_builds_direct_oauth_dcr_remote_package(tmp_path: Path):
    package = tmp_path / "plugin"

    _run(
        "--server-url",
        "https://gov.example.org/mcp/",
        "--output",
        str(package),
    )

    server = _load(package / "mcp.json")["mcpServers"]["unitares-governance"]
    assert server["type"] == "streamable-http"
    assert server["url"] == "https://gov.example.org/mcp/"
    # Agent Plugins 1.0 discovers the server's OAuth protected-resource and
    # authorization metadata at connection time. Keep this object within the
    # published schema: type, URL, and optional headers only.
    assert set(server) == {"type", "url"}
    assert "apps" not in _load(package / "plugin.json")["extensions"]["com.openai"]
    assert not (package / ".app.json").exists()
    _assert_hook_free(package)
    _assert_skills_copied(package)

    package_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in package.rglob("*")
        if path.is_file()
    )
    assert "clientSecret" not in package_text
    assert "Authorization: Bearer" not in package_text


def test_rejects_unsafe_or_non_mcp_server_urls_without_partial_output(tmp_path: Path):
    for index, url in enumerate(
        (
            "http://gov.example.org/mcp/",
            "https://user:secret@gov.example.org/mcp/",
            "https://gov.example.org/mcp/?token=secret",
            "https://gov.example.org/api",
        )
    ):
        package = tmp_path / f"plugin-{index}"
        result = _run(
            "--server-url",
            url,
            "--output",
            str(package),
            check=False,
        )
        assert result.returncode != 0
        assert not package.exists()


def test_refuses_to_overwrite_an_existing_output(tmp_path: Path):
    package = tmp_path / "plugin"
    package.mkdir()
    sentinel = package / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    result = _run(
        "--app-id",
        "plugin_asdk_app_example123",
        "--output",
        str(package),
        check=False,
    )

    assert result.returncode != 0
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_rejects_existing_or_nested_archive_before_creating_output(tmp_path: Path):
    existing_archive = tmp_path / "existing.zip"
    existing_archive.write_text("keep", encoding="utf-8")

    package = tmp_path / "plugin-existing"
    result = _run(
        "--app-id",
        "plugin_asdk_app_example123",
        "--output",
        str(package),
        "--archive",
        str(existing_archive),
        check=False,
    )
    assert result.returncode != 0
    assert not package.exists()
    assert existing_archive.read_text(encoding="utf-8") == "keep"

    nested_package = tmp_path / "plugin-nested"
    result = _run(
        "--app-id",
        "plugin_asdk_app_example123",
        "--output",
        str(nested_package),
        "--archive",
        str(nested_package / "bundle.zip"),
        check=False,
    )
    assert result.returncode != 0
    assert not nested_package.exists()
