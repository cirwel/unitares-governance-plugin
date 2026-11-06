"""Prove CI snapshot selection with actual canonical and plugin Git histories."""
import importlib.util
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("skill_source", ROOT / "scripts/dev/select-skill-source.py")
source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def commit(root, message):
    git(root, "add", ".")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def init(root):
    root.mkdir()
    git(root, "init", "-q", "-b", "master")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")


def mirror(canonical, plugin):
    if (plugin / "skills").exists():
        shutil.rmtree(plugin / "skills")
    shutil.copytree(canonical / "skills", plugin / "skills")
    import hashlib
    files = sorted(p for p in (canonical / "skills").rglob("*") if p.is_file() and ".attestations" not in p.parts)
    body = "\n".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(canonical / 'skills').as_posix()}" for p in files)
    digest = hashlib.sha256(body.encode()).hexdigest()
    (plugin / "skills" / source.MANIFEST).write_text(source.HEADER + f"# aggregate: {digest}\n{body}\n")


@pytest.fixture
def history(tmp_path):
    canonical, plugin = tmp_path / "canonical", tmp_path / "plugin"
    init(canonical); init(plugin)
    (canonical / "skills/guide").mkdir(parents=True)
    (canonical / "skills/.attestations/guide").mkdir(parents=True)
    (canonical / "skills/guide/SKILL.md").write_text("old reviewed guidance\n")
    (canonical / "skills/.attestations/guide/old.json").write_text('{"proof":"old"}\n')
    old = commit(canonical, "old canonical")
    mirror(canonical, plugin)
    base = commit(plugin, f"verified mirror\n\nVerified byte mirror of cirwel/unitares at {old}.")
    (canonical / "skills/guide/SKILL.md").write_text("new guidance needing repaired tests\n")
    (canonical / "skills/.attestations/guide/new.json").write_text('{"proof":"new"}\n')
    current = commit(canonical, "new canonical")
    git(canonical, "update-ref", "refs/remotes/origin/master", current)
    git(plugin, "update-ref", "refs/remotes/origin/master", base)
    return plugin, canonical, base, old, current


def test_test_only_repair_uses_verified_base_snapshot_despite_new_canonical(history):
    plugin, canonical, base, old, _ = history
    (plugin / "test.py").write_text("fixed tests\n")
    commit(plugin, "test repair")
    assert source.select_source(plugin, canonical, base) == old


def test_pr_message_cannot_choose_source(history):
    plugin, canonical, base, old, current = history
    (plugin / "test.py").write_text("repair\n")
    commit(plugin, f"Verified byte mirror of cirwel/unitares at {current}.")
    assert source.select_source(plugin, canonical, base) == old


def test_new_mirror_uses_current_master_for_pr_and_push(history):
    plugin, canonical, base, _, current = history
    mirror(canonical, plugin)
    landed = commit(plugin, f"Verified byte mirror of cirwel/unitares at {current}.")
    assert source.select_source(plugin, canonical, base) == current
    git(plugin, "update-ref", "refs/remotes/origin/master", landed)
    (plugin / "test.py").write_text("subsequent repair\n")
    commit(plugin, "ordinary push")
    assert source.select_source(plugin, canonical, landed) == current


@pytest.mark.parametrize("path,content", [
    ("skills/guide/SKILL.md", "tampered guidance\n"),
    ("skills/.attestations/guide/old.json", "tampered attestation\n"),
    ("skills/SKILLS_MANIFEST.sha256", "tampered manifest\n"),
])
def test_skill_mutation_cannot_select_old_snapshot(history, path, content):
    plugin, canonical, base, _, _ = history
    (plugin / path).write_text(content)
    commit(plugin, "candidate mutation")
    with pytest.raises(source.InvalidBundle, match="differ"):
        source.select_source(plugin, canonical, base)


def test_current_manifest_is_independently_verified(history):
    plugin, canonical, base, _, _ = history
    mirror(canonical, plugin)
    (plugin / "skills" / source.MANIFEST).write_text("forged aggregate\n")
    commit(plugin, "new mirror with forged manifest")
    with pytest.raises(source.InvalidBundle, match="Manifest differs"):
        source.select_source(plugin, canonical, base)


@pytest.mark.parametrize("change", ["attestation", "mode", "symlink"])
def test_partial_or_mode_changed_new_mirror_is_rejected(history, change):
    plugin, canonical, base, _, _ = history
    mirror(canonical, plugin)
    if change == "attestation":
        (plugin / "skills/.attestations/guide/new.json").unlink()
    elif change == "mode":
        (plugin / "skills/guide/SKILL.md").chmod(0o755)
    else:
        (plugin / "skills/guide/link").symlink_to("SKILL.md")
    commit(plugin, "incomplete mirror")
    with pytest.raises(source.InvalidBundle):
        source.select_source(plugin, canonical, base)


@pytest.mark.parametrize("message", ["no provenance", "Verified byte mirror of cirwel/unitares at wrong."])
def test_missing_or_malformed_landed_provenance_fails_closed(history, message):
    plugin, canonical, _, _, _ = history
    git(plugin, "commit", "--amend", "-qm", message)
    base = git(plugin, "rev-parse", "HEAD")
    git(plugin, "update-ref", "refs/remotes/origin/master", base)
    with pytest.raises(source.InvalidBundle, match="unique verified-mirror"):
        source.select_source(plugin, canonical, base)


def test_source_must_be_on_canonical_master_ancestry(history):
    plugin, canonical, base, _, _ = history
    git(canonical, "checkout", "--orphan", "unrelated")
    git(canonical, "commit", "-qm", "unrelated canonical ancestry")
    git(canonical, "update-ref", "refs/remotes/origin/master", "HEAD")
    with pytest.raises(source.InvalidBundle, match="not on canonical master ancestry"):
        source.select_source(plugin, canonical, base)


def test_landed_provenance_must_match_complete_bundle(history):
    plugin, canonical, _, _, current = history
    git(plugin, "commit", "--amend", "-qm", f"Verified byte mirror of cirwel/unitares at {current}.")
    base = git(plugin, "rev-parse", "HEAD")
    git(plugin, "update-ref", "refs/remotes/origin/master", base)
    with pytest.raises(source.InvalidBundle, match="Skills differ"):
        source.select_source(plugin, canonical, base)


@pytest.mark.parametrize("base", ["", "master", "0" * 40, "--help", "a" * 40])
def test_invalid_or_absent_base_fails_closed(history, base):
    plugin, canonical, _, _, _ = history
    with pytest.raises(source.InvalidBundle):
        source.select_source(plugin, canonical, base)


def test_ci_selects_from_event_base_before_freshness_with_full_histories():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["test"]["steps"]
    selected = next(i for i, s in enumerate(steps) if s["name"] == "Select verified canonical snapshot")
    freshness = next(i for i, s in enumerate(steps) if s["name"] == "Check skill freshness metadata")
    assert selected < freshness
    assert steps[selected]["env"]["PLUGIN_BASE"] == "${{ github.event.pull_request.base.sha || github.event.before }}"
    for step in steps[:selected]:
        if step.get("uses", "").startswith("actions/checkout@"):
            assert step["with"]["fetch-depth"] == 0


def test_feature_branch_base_is_not_trusted_landed_history(history):
    plugin, canonical, _, old, _ = history
    (plugin / "note").write_text("unlanded branch")
    unlanded = commit(plugin, f"Verified byte mirror of cirwel/unitares at {old}.")
    with pytest.raises(source.InvalidBundle, match="not on plugin master ancestry"):
        source.select_source(plugin, canonical, unlanded)


def test_merge_second_parent_cannot_supply_landed_provenance(history):
    plugin, canonical, base, _, current = history
    git(plugin, "checkout", "-qb", "feature")
    mirror(canonical, plugin)
    commit(plugin, f"Verified byte mirror of cirwel/unitares at {current}.")
    git(plugin, "checkout", "-q", "master")
    git(plugin, "merge", "--no-ff", "-qm", "landed feature without verified receipt", "feature")
    landed = git(plugin, "rev-parse", "HEAD")
    git(plugin, "update-ref", "refs/remotes/origin/master", landed)
    with pytest.raises(source.InvalidBundle, match="unique verified-mirror"):
        source.select_source(plugin, canonical, landed)


def test_real_pr_merge_preview_uses_landed_base_provenance(history):
    plugin, canonical, _, old, _ = history
    git(plugin, "checkout", "-qb", "feature")
    (plugin / "test.py").write_text("repair")
    commit(plugin, "test repair")
    git(plugin, "checkout", "-q", "master")
    (plugin / "README.md").write_text("base advanced")
    base = commit(plugin, "unrelated landed update")
    git(plugin, "update-ref", "refs/remotes/origin/master", base)
    git(plugin, "merge", "--no-ff", "-qm", "GitHub merge preview", "feature")
    assert source.select_source(plugin, canonical, base) == old


def test_reverted_mirror_needs_new_verified_receipt(history):
    plugin, canonical, _, _, current = history
    mirror(canonical, plugin)
    mirrored = commit(plugin, f"Verified byte mirror of cirwel/unitares at {current}.")
    git(plugin, "revert", "--no-edit", mirrored)
    reverted = git(plugin, "rev-parse", "HEAD")
    git(plugin, "update-ref", "refs/remotes/origin/master", reverted)
    with pytest.raises(source.InvalidBundle, match="unique verified-mirror"):
        source.select_source(plugin, canonical, reverted)


def test_forged_manifest_in_landed_base_is_rejected(history):
    plugin, canonical, _, old, _ = history
    (plugin / "skills" / source.MANIFEST).write_text("forged")
    landed = commit(plugin, f"Verified byte mirror of cirwel/unitares at {old}.")
    git(plugin, "update-ref", "refs/remotes/origin/master", landed)
    with pytest.raises(source.InvalidBundle, match="Manifest differs"):
        source.select_source(plugin, canonical, landed)
