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
        if not path.is_symlink() and not path.parent.is_symlink()
    } | {"setup-unitares"}
    actual = {path.parent.name for path in (package / "skills").glob("*/SKILL.md")}
    assert actual == expected
    assert not (package / "skills" / ".attestations").exists()
    assert not (package / "skills" / "SKILLS_MANIFEST.sha256").exists()
    expected_refs = {
        path.relative_to(ROOT / "skills").as_posix()
        for path in (ROOT / "skills").glob("*/references/*.md")
        if path.is_file()
        and not path.is_symlink()
        and not path.parent.is_symlink()
        and not path.parent.parent.is_symlink()
    }
    actual_refs = {
        path.relative_to(package / "skills").as_posix()
        for path in (package / "skills").glob("*/references/*.md")
    }
    assert actual_refs == expected_refs


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


def test_copies_skill_references_and_skips_symlinks_and_non_markdown(tmp_path: Path, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_chatgpt_plugin", BUILDER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    fake_root = tmp_path / "repo"
    (fake_root / ".codex-plugin").mkdir(parents=True)
    (fake_root / ".codex-plugin" / "plugin.json").write_bytes(
        (ROOT / ".codex-plugin" / "plugin.json").read_bytes()
    )
    (fake_root / "LICENSE").write_text("license\n", encoding="utf-8")
    (fake_root / "NOTICE").write_text("notice\n", encoding="utf-8")
    setup_dst = fake_root / "packaging" / "chatgpt" / "skills" / "setup-unitares"
    setup_dst.mkdir(parents=True)
    (setup_dst / "SKILL.md").write_text("# Setup\n", encoding="utf-8")
    secret = tmp_path / "secret.md"
    secret.write_text("do-not-bundle\n", encoding="utf-8")
    (setup_dst / "leak.md").symlink_to(secret)

    # Split skill with references/, plus a symlink, nested dir, and non-.md file to skip.
    split_skill = fake_root / "skills" / "governance-lifecycle"
    refs = split_skill / "references"
    refs.mkdir(parents=True)
    (split_skill / "SKILL.md").write_text("# Lifecycle\n", encoding="utf-8")
    (refs / "recovery.md").write_text("# Recovery\n", encoding="utf-8")
    (refs / "notes.txt").write_text("ignored\n", encoding="utf-8")
    (refs / "nested").mkdir()
    (refs / "nested" / "deep.md").write_text("nested\n", encoding="utf-8")
    (refs / "escape.md").symlink_to(secret)

    # Skill whose references/ directory is itself a symlink.
    sym_refs_skill = fake_root / "skills" / "sym-refs-skill"
    sym_refs_skill.mkdir(parents=True)
    (sym_refs_skill / "SKILL.md").write_text("# SymRefs\n", encoding="utf-8")
    ext_refs = tmp_path / "external-refs"
    ext_refs.mkdir()
    (ext_refs / "outside.md").write_text("outside\n", encoding="utf-8")
    (sym_refs_skill / "references").symlink_to(ext_refs)

    # Symlinked skill directory and skill with symlinked SKILL.md must be skipped.
    ext_skill = tmp_path / "external-skill"
    ext_skill.mkdir()
    (ext_skill / "SKILL.md").write_text("# External\n", encoding="utf-8")
    (fake_root / "skills" / "sym-skill-dir").symlink_to(ext_skill)

    sym_skill_file = fake_root / "skills" / "sym-skill-file"
    sym_skill_file.mkdir(parents=True)
    (sym_skill_file / "SKILL.md").symlink_to(secret)

    # Flat skill and attestations/manifest that must not be bundled.
    flat_skill = fake_root / "skills" / "flat-skill"
    flat_skill.mkdir(parents=True)
    (flat_skill / "SKILL.md").write_text("# Flat\n", encoding="utf-8")
    (fake_root / "skills" / ".attestations" / "governance-lifecycle").mkdir(parents=True)
    (fake_root / "skills" / ".attestations" / "governance-lifecycle" / "a.json").write_text("{}\n")
    (fake_root / "skills" / "SKILLS_MANIFEST.sha256").write_text("# manifest\n")

    monkeypatch.setattr(module, "ROOT", fake_root)
    package = tmp_path / "out"
    archive = tmp_path / "out.zip"
    module.build(
        package,
        app_id="plugin_asdk_app_example123",
        server_url=None,
        archive=archive,
    )
    stderr = capsys.readouterr().err

    assert (package / "skills" / "governance-lifecycle" / "references" / "recovery.md").read_text(
        encoding="utf-8"
    ) == "# Recovery\n"
    assert not (package / "skills" / "governance-lifecycle" / "references" / "notes.txt").exists()
    assert not (package / "skills" / "governance-lifecycle" / "references" / "escape.md").exists()
    assert not (package / "skills" / "governance-lifecycle" / "references" / "nested").exists()
    assert not (package / "skills" / "sym-refs-skill" / "references").exists()
    assert not (package / "skills" / "sym-skill-dir").exists()
    assert not (package / "skills" / "sym-skill-file").exists()
    assert not (package / "skills" / "setup-unitares" / "leak.md").exists()
    assert not (package / "skills" / "flat-skill" / "references").exists()
    assert not (package / "skills" / ".attestations").exists()
    assert not (package / "skills" / "SKILLS_MANIFEST.sha256").exists()
    assert "warning: skipping unsupported skill reference entry" in stderr
    assert "warning: skipping non-directory or symlinked references path" in stderr

    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
    assert "skills/governance-lifecycle/references/recovery.md" in names
    assert "skills/governance-lifecycle/references/escape.md" not in names
    assert "skills/governance-lifecycle/references/notes.txt" not in names
    assert "skills/setup-unitares/leak.md" not in names
