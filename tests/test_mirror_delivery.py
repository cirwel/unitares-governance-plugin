"""Exercise mirror proofs with real Git objects; writes use an in-memory API."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mirror_delivery", ROOT / "scripts/dev/merge-skill-mirror.py")
delivery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delivery)


def run(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


@pytest.fixture
def trees(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run("init", "-q")
    run("config", "user.email", "fixture@example.invalid")
    run("config", "user.name", "Fixture")
    skills = tmp_path / "skills"
    (skills / "guide").mkdir(parents=True)
    (skills / ".attestations").mkdir()
    (skills / "guide/SKILL.md").write_text("approved content\n")
    (skills / ".attestations/proof.json").write_text('{"approved":true}\n')
    run("add", "."); run("commit", "-qm", "canonical")
    canonical = run("rev-parse", "HEAD")
    (tmp_path / "README.md").write_text("plugin\n")
    run("add", "."); run("commit", "-qm", "plugin base")
    base = run("rev-parse", "HEAD")
    line = f"{hashlib.sha256(b'approved content\n').hexdigest()}  guide/SKILL.md"
    (skills / delivery.MANIFEST).write_text(
        delivery.HEADER + f"# aggregate: {hashlib.sha256(line.encode()).hexdigest()}\n{line}\n")
    run("add", "."); run("commit", "-qm", "mirror")
    head = run("rev-parse", "HEAD")
    for ref, sha in [("source", canonical), ("base", base), ("head", head)]:
        run("update-ref", f"refs/mirror/{ref}", sha)
    return canonical, head, base


def test_exact_mirror_including_attestations_and_manifest(trees):
    delivery.verify_content(*trees)


@pytest.mark.parametrize("path,content", [
    ("skills/guide/SKILL.md", "unapproved content\n"),
    ("skills/.attestations/proof.json", '{"approved":false}\n'),
    ("skills/extra.md", "extra\n"),
    ("skills/SKILLS_MANIFEST.sha256", "forged manifest\n"),
    ("README.md", "unrelated edit\n"),
])
def test_rejects_noncanonical_or_out_of_scope_changes(trees, path, content):
    Path(path).write_text(content)
    run("add", "."); run("commit", "-qm", "mutation")
    with pytest.raises(delivery.Hold):
        delivery.verify_content(trees[0], run("rev-parse", "HEAD"), trees[2])


def test_rejects_symlink_without_following_it(trees):
    Path("skills/link").symlink_to("/etc/passwd")
    run("add", "."); run("commit", "-qm", "symlink")
    with pytest.raises(delivery.Hold, match="symlink"):
        delivery.verify_content(trees[0], run("rev-parse", "HEAD"), trees[2])


def pr(head, base):
    return {"state": "open", "draft": False, "labels": [],
            "head": {"sha": head, "ref": delivery.BRANCH, "repo": {"full_name": delivery.REPO}},
            "base": {"sha": base, "ref": "master", "repo": {"full_name": delivery.REPO}}}


def ci(head, **extra):
    return {"id": 1, "path": ".github/workflows/ci.yml", "event": "pull_request",
            "head_sha": head, "head_repository": {"full_name": delivery.REPO},
            "status": "completed", "conclusion": "success", **extra}


@pytest.mark.parametrize("changes", [
    {"conclusion": "failure"}, {"status": "in_progress"}, {"head_sha": "other"},
    {"event": "push"}, {"head_repository": {"full_name": "other/fork"}},
    {"path": ".github/workflows/forged.yml"},
])
def test_ci_must_be_successful_and_cover_this_head(changes):
    with pytest.raises(delivery.Hold):
        delivery.require_checks([ci("head", **changes)], "head")


def test_old_success_cannot_override_new_failed_or_pending_run():
    with pytest.raises(delivery.Hold):
        delivery.require_checks([ci("head"), ci("head", id=2, status="in_progress")], "head")


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(draft=True),
    lambda p: p["head"].update(ref="codex/not-a-mirror"),
    lambda p: p["head"]["repo"].update(full_name="other/fork"),
    lambda p: p["head"].update(repo=None),
    lambda p: p["base"].update(ref="other"),
])
def test_candidate_rejects_drafts_forks_and_other_branches(mutate):
    p = pr("head", "base"); mutate(p)
    assert not delivery.candidate(p)


def setup_api(monkeypatch, trees, *, mutate=None, pr_base=None, moved_base=None,
              runs=None):
    _, head, base = trees
    snapshot = pr(head, pr_base or base)
    calls = []
    reads = 0
    def api(path, method="GET", body=None):
        nonlocal reads
        calls.append((path, method, body))
        if path == "branches/master":
            return {"commit": {"sha": moved_base if moved_base and reads == 2 else base}}
        if path.startswith("actions/runs"):
            return {"workflow_runs": runs if runs is not None else [ci(head)]}
        if method == "PUT":
            return {"merged": True, "sha": "merge"}
        reads += 1
        result = copy.deepcopy(snapshot)
        if mutate and reads == 2:
            mutate(result)
        return result
    actual_git = delivery.git
    monkeypatch.setattr(delivery, "git", lambda *args: b"" if args[0] == "fetch" else actual_git(*args))
    monkeypatch.setattr(delivery, "api", api)
    return calls


def test_merge_uses_verified_exact_head_and_records_source(monkeypatch, trees):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    calls = setup_api(monkeypatch, trees)
    assert "Merged #12" in delivery.deliver(12, apply=True)
    writes = [c for c in calls if c[1] == "PUT"]
    assert len(writes) == 1
    assert writes[0][0] == "pulls/12/merge"
    assert writes[0][2]["sha"] == trees[1]
    assert trees[0] in writes[0][2]["commit_message"]
    assert "/actions/runs/12345" in writes[0][2]["commit_message"]


@pytest.mark.parametrize("mutate", [
    lambda p: p["head"].update(sha="moved"),
    lambda p: p["base"].update(sha="moved"),
    lambda p: p["labels"].append({"name": "automerge-hold"}),
    lambda p: p.update(draft=True),
])
def test_raced_pr_state_never_writes(monkeypatch, trees, mutate):
    calls = setup_api(monkeypatch, trees, mutate=mutate)
    with pytest.raises(delivery.Hold, match="state changed"):
        delivery.deliver(12, apply=True)
    assert not [c for c in calls if c[1] != "GET"]


def test_dry_run_has_no_remote_writes(monkeypatch, trees):
    calls = setup_api(monkeypatch, trees)
    assert "Verified #12" in delivery.deliver(12, apply=False)
    assert not [c for c in calls if c[1] != "GET"]


def test_behind_base_updates_then_requires_another_ci_event(monkeypatch, trees):
    run("checkout", "-q", "--detach", trees[2])
    Path("README.md").write_text("base moved\n")
    run("add", "."); run("commit", "-qm", "new base")
    base = run("rev-parse", "HEAD")
    run("update-ref", "refs/mirror/base", base)
    calls = setup_api(monkeypatch, (trees[0], trees[1], base), pr_base=trees[2])
    assert "waiting for new CI" in delivery.deliver(12, apply=True)
    assert [c for c in calls if c[1] == "PUT"] == [
        ("pulls/12/update-branch", "PUT", {"expected_head_sha": trees[1]})]


def advance_base(trees):
    """Move plugin master past the mirror head, as a fix landing on master would."""
    run("checkout", "-q", "--detach", trees[2])
    Path("README.md").write_text("base moved\n")
    run("add", "."); run("commit", "-qm", "new base")
    base = run("rev-parse", "HEAD")
    run("update-ref", "refs/mirror/base", base)
    return (trees[0], trees[1], base)


def failed(head):
    return [ci(head, conclusion="failure")]


def test_failed_ci_on_older_base_updates_branch_without_merging(monkeypatch, trees):
    moved = advance_base(trees)
    calls = setup_api(monkeypatch, moved, pr_base=trees[2], runs=failed(trees[1]))
    assert "re-running on current master" in delivery.deliver(12, apply=True)
    assert [c for c in calls if c[1] != "GET"] == [
        ("pulls/12/update-branch", "PUT", {"expected_head_sha": trees[1]})]


def test_failed_ci_on_current_base_stays_held(monkeypatch, trees):
    calls = setup_api(monkeypatch, trees, runs=failed(trees[1]))
    with pytest.raises(delivery.Hold, match="current base"):
        delivery.deliver(12, apply=True)
    assert not [c for c in calls if c[1] != "GET"]


def test_failed_ci_dry_run_has_no_remote_writes(monkeypatch, trees):
    moved = advance_base(trees)
    calls = setup_api(monkeypatch, moved, pr_base=trees[2], runs=failed(trees[1]))
    assert "action=update base" in delivery.deliver(12, apply=False)
    assert not [c for c in calls if c[1] != "GET"]


def test_pending_ci_never_updates_branch(monkeypatch, trees):
    moved = advance_base(trees)
    calls = setup_api(monkeypatch, moved, pr_base=trees[2],
                      runs=[ci(trees[1], status="in_progress")])
    with pytest.raises(delivery.Hold, match="has not passed"):
        delivery.deliver(12, apply=True)
    assert not [c for c in calls if c[1] != "GET"]


def test_failed_noncanonical_mirror_is_never_updated(monkeypatch, trees):
    run("checkout", "-q", "--detach", trees[1])
    Path("skills/guide/SKILL.md").write_text("unapproved content\n")
    run("add", "."); run("commit", "-qm", "mutation")
    head = run("rev-parse", "HEAD")
    run("update-ref", "refs/mirror/head", head)
    moved = advance_base(trees)
    calls = setup_api(monkeypatch, (trees[0], head, moved[2]), pr_base=trees[2],
                      runs=failed(head))
    with pytest.raises(delivery.Hold):
        delivery.deliver(12, apply=True)
    assert not [c for c in calls if c[1] != "GET"]


def test_failed_ci_respects_hold_label_added_during_verification(monkeypatch, trees):
    moved = advance_base(trees)
    calls = setup_api(monkeypatch, moved, pr_base=trees[2], runs=failed(trees[1]),
                      mutate=lambda p: p["labels"].append({"name": "automerge-hold"}))
    with pytest.raises(delivery.Hold, match="state changed"):
        delivery.deliver(12, apply=True)
    assert not [c for c in calls if c[1] != "GET"]


def test_current_base_race_never_writes(monkeypatch, trees):
    calls = setup_api(monkeypatch, trees, moved_base="new-master")
    with pytest.raises(delivery.Hold, match="state changed"):
        delivery.deliver(12, apply=True)
    assert not [c for c in calls if c[1] != "GET"]


class Tracker:
    """In-memory issues API: one mirror PR whose head was committed `age` ago."""

    def __init__(self, monkeypatch, *, age=0, issues=(), fail_issues=False):
        self.calls, self.issues, self.fail_issues = [], list(issues), fail_issues
        stamp = datetime.now(timezone.utc) - timedelta(seconds=age)
        self.date = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        monkeypatch.setattr(delivery, "api", self.api)

    def api(self, path, method="GET", body=None):
        self.calls.append((path, method, body))
        if path.startswith("pulls?"):
            return [pr("head", "base") | {"number": 12}]
        if path == "pulls/12":
            return pr("head", "base")
        if path == "commits/head":
            return {"commit": {"committer": {"date": self.date}}}
        if self.fail_issues and "issues" in path:
            raise delivery.Hold("GitHub issues failed")
        if path.startswith("issues?"):
            return self.issues + [{"number": 99, "body": delivery.STUCK_MARKER, "pull_request": {}}]
        if path == "issues" and method == "POST":
            return {"number": 7}
        return {}

    @property
    def writes(self):
        return [c for c in self.calls if c[1] != "GET"]


def hold_with(monkeypatch, exc):
    def deliver(number, *, apply):
        raise exc
    monkeypatch.setattr(delivery, "deliver", deliver)


RED = "CI failed on the current base; the mirror needs a fix on master"


def test_red_mirror_on_current_base_opens_one_issue(monkeypatch, capsys):
    tracker = Tracker(monkeypatch)
    hold_with(monkeypatch, delivery.Stuck(RED))
    assert delivery.main(["--apply"]) == 1
    created = [c for c in tracker.writes if c[0] == "issues"]
    assert len(created) == 1
    assert created[0][2]["labels"] == [delivery.STUCK_LABEL]
    assert delivery.STUCK_MARKER in created[0][2]["body"]
    assert "#12" in created[0][2]["body"] and RED in created[0][2]["body"]
    assert "Opened stuck-mirror issue #7" in capsys.readouterr().out


def open_issue(reason, head="head"):
    return {"number": 7, "body": f"{delivery.STUCK_MARKER}\n<!-- stuck-key: {head}:{reason} -->\n"}


def test_unchanged_stuck_mirror_writes_nothing_on_later_runs(monkeypatch):
    tracker = Tracker(monkeypatch, issues=[open_issue(RED)])
    hold_with(monkeypatch, delivery.Stuck(RED))
    assert delivery.main(["--apply"]) == 1
    assert not tracker.writes


def test_new_reason_or_head_rewrites_the_open_issue(monkeypatch):
    tracker = Tracker(monkeypatch, issues=[open_issue(RED, head="older")])
    hold_with(monkeypatch, delivery.Stuck(RED))
    delivery.main(["--apply"])
    assert [(c[0], c[1]) for c in tracker.writes] == [("issues/7", "PATCH")]
    assert "stuck-key: head:" in tracker.writes[0][2]["body"]


@pytest.mark.parametrize("age,reported", [
    (60, False), (delivery.STUCK_AFTER_SECONDS + 60, True)])
def test_ordinary_hold_is_reported_only_once_the_head_has_sat(monkeypatch, age, reported):
    tracker = Tracker(monkeypatch, age=age)
    hold_with(monkeypatch, delivery.Hold("The latest CI run for this exact PR head has not passed"))
    assert delivery.main(["--apply"]) == 1
    assert bool(tracker.writes) is reported


def test_maintainer_hold_label_is_never_reported(monkeypatch):
    tracker = Tracker(monkeypatch, age=delivery.STUCK_AFTER_SECONDS * 10)
    hold_with(monkeypatch, delivery.Vetoed("A maintainer hold label is present"))
    assert delivery.main(["--apply"]) == 1
    assert not tracker.writes


def test_hold_label_raises_the_unreported_kind(monkeypatch):
    labelled = pr("head", "base") | {"labels": [{"name": "automerge-hold"}]}
    monkeypatch.setattr(delivery, "api", lambda *a, **k: labelled)
    with pytest.raises(delivery.Vetoed):
        delivery.deliver(12, apply=True)


def test_failed_ci_on_current_base_is_the_immediately_reported_kind(monkeypatch, trees):
    setup_api(monkeypatch, trees, runs=failed(trees[1]))
    with pytest.raises(delivery.Stuck):
        delivery.deliver(12, apply=True)


@pytest.mark.parametrize("message,closes", [
    ("Merged #12: canonical a, PR head b, merge c", True),
    ("Updated #12 to the current base; waiting for new CI before merging", False),
])
def test_issue_closes_when_the_mirror_lands_not_when_it_is_retried(monkeypatch, message, closes):
    tracker = Tracker(monkeypatch, issues=[open_issue(RED)])
    monkeypatch.setattr(delivery, "deliver", lambda number, *, apply: message)
    assert delivery.main(["--apply"]) == 0
    expected = [("issues/7", "PATCH", {"state": "closed", "state_reason": "completed"})]
    assert tracker.writes == (expected if closes else [])


def test_dry_run_never_reads_or_writes_issues(monkeypatch):
    tracker = Tracker(monkeypatch, age=delivery.STUCK_AFTER_SECONDS * 10)
    hold_with(monkeypatch, delivery.Stuck(RED))
    assert delivery.main([]) == 1
    assert not [c for c in tracker.calls if "issues" in c[0] or "commits" in c[0]]


def test_issue_failure_does_not_change_the_delivery_result(monkeypatch, capsys):
    Tracker(monkeypatch, fail_issues=True)
    monkeypatch.setattr(delivery, "deliver", lambda number, *, apply: "Merged #12: a")
    assert delivery.main(["--apply"]) == 0
    assert "could not be read or written" in capsys.readouterr().out


def test_privileged_workflow_uses_only_trusted_code_and_scoped_token():
    workflow = yaml.safe_load((ROOT / ".github/workflows/mirror-delivery.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert "pull_request_target" not in triggers and "pull_request" not in triggers
    assert triggers["workflow_run"]["workflows"] == ["CI"]
    job = workflow["jobs"]["deliver"]
    assert job["environment"] == "mirror-delivery"
    checkout, app, execute = job["steps"]
    assert checkout["with"]["ref"] == "master"
    assert checkout["with"]["persist-credentials"] is False
    assert app["with"]["repositories"] == "unitares-governance-plugin"
    assert "permission-workflows" not in app["with"]
    assert app["with"]["permission-issues"] == "write"
    assert execute["run"] == "python3 scripts/dev/merge-skill-mirror.py --apply"
