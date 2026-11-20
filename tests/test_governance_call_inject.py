"""Contract tests for the pre-governance-call PreToolUse injector.

The injector adds the slot-cached client_session_id to governance MCP calls
that carry no identity arguments, so driver calls resolve at tier=strong
instead of through the displaceable IP:UA onboard pin. See
scripts/governance_call_inject.py for the full contract.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
HOOK = PLUGIN_ROOT / "hooks" / "pre-governance-call"
SLOT = "test-session-abc"
SID = "agent-cafe1234-aaa"

sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from governance_call_inject import (  # noqa: E402
    ANCHORED_MINT_SUFFIXES,
    CODEX_REWRITE_SUFFIXES,
    IDENTITY_ASSERT_SUFFIXES,
    INJECT_SUFFIXES,
)
from tag_normalize import TAG_BEARING_SUFFIXES  # noqa: E402


def _write_cache(workspace: Path, slot: str = SLOT, sid: str = SID) -> None:
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in slot)[:64]
    payload = json.dumps({
        "uuid": "cafe1234-0000-0000-0000-000000000000",
        "client_session_id": sid,
        "schema_version": 2,
        "slot": slot,
    })
    for cache_dir in {workspace / ".unitares", Path.home() / ".unitares"}:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"session-{safe}.json"
        path.write_text(payload)
        path.chmod(0o600)


def _run(hook_input: dict, workspace: Path, *, host: str | None = None):
    command = [str(HOOK)]
    if host is not None:
        command.extend(("--host", host))
    return subprocess.run(
        command,
        input=json.dumps(hook_input),
        text=True,
        capture_output=True,
        timeout=10,
        cwd=str(workspace),
    )


def _hook_input(tool_name: str, tool_input: dict | None = None, slot: str = SLOT) -> dict:
    return {
        "session_id": slot,
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input if tool_input is not None else {},
    }


def _updated_input(result) -> dict | None:
    out = result.stdout.strip()
    if not out:
        return None
    return json.loads(out)["hookSpecificOutput"]["updatedInput"]


class TestInjection:

    def test_injects_into_argless_checkin(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__process_agent_update",
            {"response_text": "did work"},
        ), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["client_session_id"] == SID
        assert updated["response_text"] == "did work"  # original fields echoed

    def test_injects_for_alias_tool(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input("mcp__unitares-governance__sync_state", {}), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["client_session_id"] == SID

    def test_inherited_home_binding_is_not_injected(self, tmp_path):
        _write_cache(tmp_path)
        cache = Path.home() / ".unitares" / f"session-{SLOT}.json"
        payload = json.loads(cache.read_text(encoding="utf-8"))
        payload["prior_process"] = True
        cache.write_text(json.dumps(payload), encoding="utf-8")
        cache.chmod(0o600)

        result = _run(
            _hook_input("mcp__unitares-governance__sync_state", {}),
            tmp_path,
        )
        assert _updated_input(result) is None

    @pytest.mark.parametrize("tool,args", [
        ("consult", {"brief": "question", "host_ids": ["a", "b"]}),
        ("use_tool", {"tool_name": "update_finding", "arguments": {"discovery_id": "x"}}),
    ])
    def test_injects_into_advertised_gateway_tools(self, tmp_path, tool, args):
        # Uninjected, these fell back to the server's fingerprint guess and could
        # bind to a neighbouring session (observed 2026-10-20 on consult).
        _write_cache(tmp_path)
        result = _run(_hook_input(f"mcp__unitares-governance__{tool}", args), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["client_session_id"] == SID
        assert {k: updated[k] for k in args} == args

    @pytest.mark.parametrize("target", [
        "identity", "onboard", "start_session", "bind_session",  # exclusions
        "use_tool",                                              # recursion
        "call_model",                                            # not injectable
    ])
    def test_gateway_never_carries_the_session_to_an_excluded_target(self, tmp_path, target):
        # The server forwards an outer client_session_id into the target's
        # arguments, so injecting here would bypass the direct-call exclusions.
        _write_cache(tmp_path)
        result = _run(_hook_input("mcp__unitares-governance__use_tool",
                                  {"tool_name": target, "arguments": {}}), tmp_path)
        assert _updated_input(result) is None

    @pytest.mark.parametrize("field", ["agent_id", "client_session_id", "agent_uuid", "continuity_token"])
    def test_gateway_honours_nested_identity_intent(self, tmp_path, field):
        _write_cache(tmp_path)
        result = _run(_hook_input("mcp__unitares-governance__use_tool", {
            "tool_name": "check_working_state", "arguments": {field: "explicit"}}), tmp_path)
        if field in {"client_session_id", "continuity_token"}:
            decision = json.loads(result.stdout)["hookSpecificOutput"]
            assert decision["permissionDecision"] == "deny"
        else:
            assert _updated_input(result) is None

    @pytest.mark.parametrize("tool_input", [
        {"arguments": {}},                                    # no target named
        {"tool_name": "sync_state", "arguments": "not-a-dict"},
    ])
    def test_gateway_with_malformed_input_is_left_alone(self, tmp_path, tool_input):
        _write_cache(tmp_path)
        result = _run(_hook_input("mcp__unitares-governance__use_tool", tool_input), tmp_path)
        assert _updated_input(result) is None

    def test_injects_for_codex_governance_server_alias(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input("mcp__governance__sync_state", {}), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["client_session_id"] == SID

    def test_injects_for_legacy_unitares_server_alias(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input("mcp__UNITARES__sync_state", {}), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["client_session_id"] == SID

    def test_injects_for_gateway_server_name(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input("mcp__claude_ai_UNITARES__knowledge", {"action": "search"}), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["client_session_id"] == SID
        assert updated["action"] == "search"

    def test_injects_for_native_claude_plugin_scoped_server(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(
            _hook_input(
                "mcp__plugin_unitares-governance_unitares-governance__sync_state",
                {},
            ),
            tmp_path,
            host="claude",
        )
        updated = _updated_input(result)
        assert updated is not None
        assert updated["client_session_id"] == SID

    def test_no_permission_decision_emitted(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__process_agent_update", {}), tmp_path)
        payload = json.loads(result.stdout.strip())
        assert "permissionDecision" not in payload["hookSpecificOutput"]

    def test_codex_rewrite_includes_required_allow_decision(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(
            _hook_input("mcp__unitares-governance__sync_state", {}),
            tmp_path,
            host="codex",
        )
        payload = json.loads(result.stdout.strip())["hookSpecificOutput"]
        assert payload["permissionDecision"] == "allow"
        assert payload["updatedInput"]["client_session_id"] == SID

    def test_codex_legacy_alias_is_not_rewritten_or_preapproved(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(
            _hook_input("mcp__governance__sync_state", {}),
            tmp_path,
            host="codex",
        )

        assert result.returncode == 0
        assert result.stdout.strip() == ""


class TestTagNormalization:

    def test_normalizes_tags_and_injects_identity_together(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__knowledge",
            {"action": "note", "tags": ["Postgres", "DB_Pool", "postgres"]},
        ), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        # tags normalized + de-duped
        assert updated["tags"] == ["postgres", "db-pool"]
        # identity still injected in the same updatedInput
        assert updated["client_session_id"] == SID
        assert updated["action"] == "note"

    def test_normalizes_tags_even_when_identity_injection_skipped(self, tmp_path):
        # Proof field present => identity injection skipped, but tag
        # normalization must still happen.
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__knowledge",
            {"action": "search", "tags": ["Postgres"], "agent_id": "some-uuid"},
        ), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["tags"] == ["postgres"]
        # explicit proof field preserved, no injected session id
        assert updated["agent_id"] == "some-uuid"
        assert "client_session_id" not in updated

    def test_normalizes_tags_with_no_cache(self, tmp_path):
        # No session cache => no identity injection, but tags still normalize.
        result = _run(_hook_input(
            "mcp__unitares-governance__leave_note",
            {"tags": ["Foo_Bar", "foo-bar"]},
        ), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["tags"] == ["foo-bar"]

    def test_already_canonical_tags_no_output_without_injection(self, tmp_path):
        # Canonical tags + a proof field => nothing to change => empty output.
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__knowledge",
            {"action": "search", "tags": ["postgres"], "agent_id": "u"},
        ), tmp_path)
        assert result.stdout.strip() == ""

    @pytest.mark.parametrize("tool", ["store_finding", "update_finding"])
    def test_shared_memory_write_aliases_normalize_tags_and_inject(self, tmp_path, tool):
        # store_finding / update_finding are the workflow names for
        # knowledge(action="store"/"update"); they get the same tag
        # formatting and identity injection as the router they alias.
        _write_cache(tmp_path)
        result = _run(_hook_input(
            f"mcp__unitares-governance__{tool}",
            {"summary": "found it", "tags": ["PostgreSQL", "DB_Pool", "postgres"]},
        ), tmp_path, host="claude")
        payload = json.loads(result.stdout.strip())["hookSpecificOutput"]
        assert "permissionDecision" not in payload
        updated = payload["updatedInput"]
        assert updated["tags"] == ["postgres", "db-pool"]
        assert updated["client_session_id"] == SID
        assert updated["summary"] == "found it"

    def test_tags_untouched_on_non_tag_bearing_tool(self, tmp_path):
        # process_agent_update is not tag-bearing; a stray tags field is
        # passed through unchanged (only identity is injected).
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__process_agent_update",
            {"tags": ["Postgres"]},
        ), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["tags"] == ["Postgres"]  # not normalized
        assert updated["client_session_id"] == SID


class TestAnchoredMintingTools:

    def test_anchored_start_session_without_force_new_injects_env_anchor(self, tmp_path, monkeypatch):
        # Mirrors the anchored-session banner contract: a forgetful agent's
        # bare start_session() resumes through the per-thread anchor instead
        # of falling back to server-side pin/name heuristics.
        monkeypatch.setenv("UNITARES_CLIENT_SESSION_ID", "agent:/thread-123")
        monkeypatch.setenv("UNITARES_ORCHESTRATED", "1")
        monkeypatch.setenv("UNITARES_ORCHESTRATED_OWNER_PID", "12345")
        monkeypatch.setenv("UNITARES_HOOK_HOST_PID", "12345")
        result = _run(_hook_input(
            "mcp__unitares-governance__start_session", {}), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["client_session_id"] == "agent:/thread-123"
        assert updated["orchestrated"] is True

    def test_anchored_onboard_without_force_new_injects_env_anchor(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNITARES_CLIENT_SESSION_ID", "agent:/thread-123")
        monkeypatch.setenv("UNITARES_ORCHESTRATED", "yes")
        monkeypatch.setenv("UNITARES_ORCHESTRATED_OWNER_PID", "12345")
        monkeypatch.setenv("UNITARES_HOOK_HOST_PID", "12345")
        result = _run(_hook_input(
            "mcp__unitares-governance__onboard", {"name": "claude-thread"}), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["name"] == "claude-thread"
        assert updated["client_session_id"] == "agent:/thread-123"
        assert updated["orchestrated"] is True

    def test_unanchored_bare_start_session_stays_bare(self, tmp_path, monkeypatch):
        monkeypatch.delenv("UNITARES_CLIENT_SESSION_ID", raising=False)
        result = _run(_hook_input(
            "mcp__unitares-governance__start_session", {}), tmp_path)
        assert result.stdout.strip() == ""

    def test_unmarked_anchor_cannot_first_bind(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNITARES_CLIENT_SESSION_ID", "agent:/thread-123")
        monkeypatch.setenv("UNITARES_ORCHESTRATED", "1")
        monkeypatch.delenv("UNITARES_ORCHESTRATED_OWNER_PID", raising=False)
        result = _run(_hook_input("mcp__unitares-governance__start_session", {}), tmp_path)
        assert result.stdout.strip() == ""

    def test_direct_hook_rejects_mismatched_owner(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNITARES_CLIENT_SESSION_ID", "agent:/thread-123")
        monkeypatch.setenv("UNITARES_ORCHESTRATED", "1")
        monkeypatch.setenv("UNITARES_ORCHESTRATED_OWNER_PID", "12345")
        monkeypatch.delenv("UNITARES_HOOK_HOST_PID", raising=False)
        result = _run(_hook_input("mcp__unitares-governance__start_session", {}), tmp_path)
        assert result.stdout.strip() == ""

    def test_leaked_anchor_without_orchestration_marker_stays_bare(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNITARES_CLIENT_SESSION_ID", "agent:/leaked-anchor")
        monkeypatch.delenv("UNITARES_ORCHESTRATED", raising=False)
        result = _run(
            _hook_input("mcp__unitares-governance__start_session", {}),
            tmp_path,
            host="codex",
        )
        assert result.stdout.strip() == ""

    def test_explicit_force_new_blocks_anchor_injection(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNITARES_CLIENT_SESSION_ID", "agent:/thread-123")
        result = _run(_hook_input(
            "mcp__unitares-governance__start_session", {"force_new": True}), tmp_path)
        assert result.stdout.strip() == ""

    @pytest.mark.parametrize("declared", [
        {"parent_agent_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"},
        {"spawn_reason": "subagent"},
        {"orchestrated": False},
        {"resume": False},
    ])
    def test_declared_child_or_explicit_mode_is_not_rewritten(self, tmp_path, monkeypatch, declared):
        monkeypatch.setenv("UNITARES_CLIENT_SESSION_ID", "agent:/thread-123")
        monkeypatch.setenv("UNITARES_ORCHESTRATED", "1")
        monkeypatch.setenv("UNITARES_ORCHESTRATED_OWNER_PID", "12345")
        monkeypatch.setenv("UNITARES_HOOK_HOST_PID", "12345")
        result = _run(_hook_input("mcp__unitares-governance__start_session", declared), tmp_path)
        assert result.stdout.strip() == ""

    @pytest.mark.parametrize("force_new", [False, "false", "0"])
    def test_false_force_new_still_attaches_first_bind(self, tmp_path, monkeypatch, force_new):
        monkeypatch.setenv("UNITARES_CLIENT_SESSION_ID", "agent:/thread-123")
        monkeypatch.setenv("UNITARES_ORCHESTRATED", "1")
        monkeypatch.setenv("UNITARES_ORCHESTRATED_OWNER_PID", "12345")
        monkeypatch.setenv("UNITARES_HOOK_HOST_PID", "12345")
        result = _run(_hook_input(
            "mcp__unitares-governance__start_session", {"force_new": force_new}), tmp_path)
        assert _updated_input(result)["orchestrated"] is True

    @pytest.mark.parametrize("host,manifest,root_var", [
        ("claude", "claude-hooks.json", "CLAUDE_PLUGIN_ROOT"),
        ("codex", "codex-hooks.json", "PLUGIN_ROOT"),
    ])
    def test_manifest_command_scopes_anchor_to_launched_cli(
        self, tmp_path, host, manifest, root_var,
    ):
        payload = json.dumps(_hook_input("mcp__unitares-governance__start_session", {}))
        entries = json.loads((PLUGIN_ROOT / "hooks" / manifest).read_text())["hooks"]["PreToolUse"]
        command = next(entry["hooks"][0]["command"] for entry in entries
                       if "pre-governance-call" in str(entry))
        env = {
            **os.environ,
            root_var: str(PLUGIN_ROOT),
            "UNITARES_CLIENT_SESSION_ID": "agent:/thread-123",
            "UNITARES_ORCHESTRATED": "1",
            "UNITARES_ORCHESTRATED_OWNER_PID": str(os.getpid()),
        }
        owner = subprocess.run(
            ["/bin/sh", "-c", command],
            input=payload, text=True, capture_output=True, cwd=tmp_path, env=env, timeout=10,
        )
        assert owner.returncode == 0
        assert owner.stderr.strip() == ""
        assert _updated_input(owner)["orchestrated"] is True

        # A reviewer CLI launched by that host inherits the same env, but its
        # hook-host PID differs. No thread anchor reaches its governance call.
        env["UNITARES_ORCHESTRATED_OWNER_PID"] = "1"
        child = subprocess.run(
            ["/bin/sh", "-c", command],
            input=payload, text=True, capture_output=True, cwd=tmp_path, env=env, timeout=10,
        )
        assert child.returncode == 0
        assert child.stdout.strip() == ""

        env.pop("UNITARES_ORCHESTRATED_OWNER_PID")
        unmarked = subprocess.run(
            ["/bin/sh", "-c", command],
            input=payload, text=True, capture_output=True, cwd=tmp_path, env=env, timeout=10,
        )
        assert unmarked.returncode == 0
        assert unmarked.stdout.strip() == ""

    def test_explicit_proof_field_blocks_anchor_injection(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNITARES_CLIENT_SESSION_ID", "agent:/thread-123")
        result = _run(_hook_input(
            "mcp__unitares-governance__start_session",
            {"client_session_id": "agent:/explicit"},
        ), tmp_path)
        assert result.stdout.strip() == ""

    def test_codex_fallback_skips_hook_runner_without_owner_check(self, tmp_path):
        cache = tmp_path / "cache"
        legacy = cache / "0.4.9" / "hooks" / "run-hook.cmd"
        safe = cache / "0.4.31" / "hooks" / "run-hook.cmd"
        for path in (legacy, safe):
            path.parent.mkdir(parents=True)
            path.write_text(
                "#!/bin/sh\n"
                + ("# UNITARES_ORCHESTRATED_OWNER_PID\n" if path == safe else "")
                + f"printf '%s\\n' {'safe' if path == safe else 'legacy'}\n"
            )
            path.chmod(0o755)

        entries = json.loads((PLUGIN_ROOT / "hooks" / "codex-hooks.json").read_text())["hooks"]["PreToolUse"]
        command = next(entry["hooks"][0]["command"] for entry in entries
                       if "pre-governance-call" in str(entry))
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            env={"PATH": os.environ.get("PATH", ""), "PLUGIN_ROOT": str(cache / "removed")},
            text=True, capture_output=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "safe"


class TestExclusions:

    def test_codex_does_not_rewrite_admin_capable_tool(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(
            _hook_input("mcp__governance__config", {"action": "get"}),
            tmp_path,
            host="codex",
        )
        assert result.stdout.strip() == ""

    @pytest.mark.parametrize(
        "tool", ["knowledge", "leave_note", "store_finding", "update_finding"]
    )
    def test_codex_does_not_rewrite_shared_memory_writes(self, tmp_path, tool):
        # A Codex rewrite must carry permissionDecision=allow, so rewriting a
        # durable shared-memory write would pre-approve it. These calls keep
        # the normal permission flow and must carry identity explicitly.
        _write_cache(tmp_path)
        result = _run(
            _hook_input(
                f"mcp__unitares-governance__{tool}",
                {"summary": "found it", "tags": ["Postgres"]},
            ),
            tmp_path,
            host="codex",
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_never_injects_into_onboard(self, tmp_path):
        # Explicit force_new keeps its fresh-mint semantics even when the
        # minting tool has an anchored-session exception for bare calls.
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__onboard",
            {"force_new": True, "spawn_reason": "explicit"}), tmp_path)
        assert result.stdout.strip() == ""

    def test_never_injects_into_start_session(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__start_session",
            {"force_new": True, "spawn_reason": "explicit"}), tmp_path)
        assert result.stdout.strip() == ""

    def test_never_injects_into_bind_session(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__bind_session", {}), tmp_path)
        assert result.stdout.strip() == ""

    def test_never_injects_into_identity(self, tmp_path):
        # identity() with a client_session_id is a resume/assert signal; a
        # bare identity() must never be rewritten to one or the post-identity
        # hook could cache a resumed (possibly stale) identity. A bound slot
        # denies the bare call instead because the server otherwise promotes
        # it to force_new=true.
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__identity", {}), tmp_path)
        out = json.loads(result.stdout)["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "updatedInput" not in out

    def test_skips_non_unitares_server(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input("mcp__GitHub__agent", {}), tmp_path)
        assert result.stdout.strip() == ""

    def test_codex_skips_unitares_lookalike_without_preapproval(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(
            _hook_input("mcp__evil-unitares-proxy__sync_state", {}),
            tmp_path,
            host="codex",
        )
        assert result.stdout.strip() == ""

    def test_skips_non_mcp_tool(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input("Bash", {"command": "ls"}), tmp_path)
        assert result.stdout.strip() == ""

    def test_skips_unknown_suffix(self, tmp_path):
        # New/unknown tools degrade to pin behavior, never risk an
        # extra-field validation error.
        _write_cache(tmp_path)
        result = _run(_hook_input("mcp__unitares-governance__skills", {}), tmp_path)
        assert result.stdout.strip() == ""


class TestProofFieldsRespectSlotAuthority:

    def test_matching_explicit_client_session_id_passes(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__process_agent_update",
            {"client_session_id": SID}), tmp_path)
        assert result.stdout.strip() == ""

    def test_foreign_explicit_client_session_id_is_denied(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__process_agent_update",
            {"client_session_id": "agent-foreign-111"}), tmp_path)
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert "protected current-slot identity" in decision["permissionDecisionReason"]

    def test_explicit_agent_id_wins(self, tmp_path):
        # A call naming its target carries its own proof signal — a
        # contract-following subagent passing agent_id is never captured.
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__agent",
            {"action": "get", "agent_id": "some-uuid"}), tmp_path)
        assert result.stdout.strip() == ""

    def test_explicit_continuity_token_wins(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__identity", {"continuity_token": "v1.tok"}), tmp_path)
        assert result.stdout.strip() == ""

    def test_empty_string_proof_field_does_not_block(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__process_agent_update",
            {"client_session_id": ""}), tmp_path)
        updated = _updated_input(result)
        assert updated is not None
        assert updated["client_session_id"] == SID


BUNDLED = "mcp__plugin_unitares-governance_unitares-governance__"
# Deep enough that json.loads fails on every supported Python (3.14 decodes
# 100k levels; it still overflows its stack at a few million).
_TOO_DEEP = 3_000_000


def _deny_reason(result) -> str | None:
    out = result.stdout.strip()
    if not out:
        return None
    specific = json.loads(out)["hookSpecificOutput"]
    if specific.get("permissionDecision") != "deny":
        return None
    return specific["permissionDecisionReason"]


class TestBundledCleartextTransport:
    """Proof never goes to the bundled server over non-loopback cleartext."""

    @pytest.fixture(autouse=True)
    def _no_anchor(self, monkeypatch):
        for name in (
            "UNITARES_ORCHESTRATED",
            "UNITARES_CLIENT_SESSION_ID",
            "UNITARES_ORCHESTRATED_OWNER_PID",
            "UNITARES_HOOK_HOST_PID",
        ):
            monkeypatch.delenv(name, raising=False)

    @pytest.mark.parametrize(
        ("tool_input", "bound"),
        [
            ({}, True),  # the hook would inject the slot's client_session_id
            ({"client_session_id": SID}, True),
            ({"continuity_token": "v1.tok"}, False),
        ],
    )
    def test_proof_bearing_call_is_denied(self, tmp_path, monkeypatch, tool_input, bound):
        monkeypatch.setenv("UNITARES_SERVER_URL", "http://gov.example.test")
        if bound:
            _write_cache(tmp_path)
        result = _run(_hook_input(f"{BUNDLED}sync_state", tool_input), tmp_path,
                      host="claude")
        reason = _deny_reason(result)
        assert reason is not None
        assert "cleartext HTTP" in reason
        assert "gov.example.test" not in reason

    def test_gateway_nested_proof_is_denied(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNITARES_SERVER_URL", "http://192.0.2.1:8767")
        result = _run(_hook_input(f"{BUNDLED}use_tool", {
            "tool_name": "identity",
            "arguments": {"client_session_id": SID},
        }), tmp_path, host="claude")
        assert _deny_reason(result) is not None

    @pytest.mark.parametrize(
        ("suffix", "tool_input"),
        [
            ("sync_state", {"kwargs": {"client_session_id": SID}}),
            ("sync_state", {"kwargs": json.dumps({"client_session_id": SID})}),
            ("sync_state", {"kwargs": {"kwargs": {"continuity_token": "v1.tok"}}}),
            ("use_tool", {"tool_name": "sync_state",
                          "arguments": {"kwargs": {"client_session_id": SID}}}),
            ("use_tool", {"tool_name": "sync_state",
                          "arguments": json.dumps({"client_session_id": SID})}),
            ("use_tool", {"tool_name": "sync_state",
                          "arguments": [{"client_session_id": SID}]}),
            ("use_tool", {"kwargs": {"tool_name": "sync_state",
                                     "arguments": {"client_session_id": SID}}}),
        ],
    )
    def test_wrapped_agent_proof_is_denied(self, tmp_path, monkeypatch, suffix, tool_input):
        monkeypatch.setenv("UNITARES_SERVER_URL", "http://gov.example.test")
        result = _run(_hook_input(f"{BUNDLED}{suffix}", tool_input), tmp_path,
                      host="claude")
        assert "cleartext HTTP" in (_deny_reason(result) or "")

    @pytest.mark.parametrize(
        "tool_input",
        [
            {"kwargs": {"query": "x"}},
            {"kwargs": "not json"},
            {"kwargs": json.dumps({"query": "x"})},
        ],
    )
    def test_wrapped_proofless_call_passes(self, tmp_path, monkeypatch, tool_input):
        monkeypatch.setenv("UNITARES_SERVER_URL", "http://gov.example.test")
        result = _run(_hook_input(f"{BUNDLED}search_shared_memory", tool_input),
                      tmp_path, host="claude")
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_deny_survives_an_injection_error(self, monkeypatch, capsys):
        import io

        import governance_call_inject as inject

        def _boom(*_args, **_kwargs):
            raise RuntimeError("injection failed")

        monkeypatch.setenv("UNITARES_SERVER_URL", "http://gov.example.test")
        monkeypatch.setattr(inject, "_decide", _boom)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_hook_input(
            f"{BUNDLED}sync_state", {"client_session_id": SID}))))
        assert inject.main(["--host", "claude"]) == 0
        output = json.loads(capsys.readouterr().out)
        specific = output["hookSpecificOutput"]
        assert specific["permissionDecision"] == "deny"
        assert "cleartext HTTP" in specific["permissionDecisionReason"]

    def test_agent_supplied_proof_on_unlisted_suffix_is_denied(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNITARES_SERVER_URL", "http://gov.example.test")
        result = _run(_hook_input(f"{BUNDLED}bind_session", {
            "client_session_id": SID,
        }), tmp_path, host="claude")
        assert _deny_reason(result) is not None

    @pytest.mark.parametrize(
        "suffix", ["mark_response_complete", "get_dialectic_session", "some_future_tool"]
    )
    def test_bundled_tool_outside_the_named_list_is_routed_and_denied(
        self, tmp_path, monkeypatch, suffix
    ):
        # Full tool advertisement exposes tools the governance matcher does
        # not name. The transport check has to run for every tool on the
        # bundled server, so the manifest must route all of them to the hook.
        tool_name = f"{BUNDLED}{suffix}"
        matchers = _pre_governance_matchers("hooks/claude-hooks.json", "claude")
        assert any(re.fullmatch(m, tool_name) for m in matchers), matchers
        assert any(re.search(m, tool_name) for m in matchers), matchers
        monkeypatch.setenv("UNITARES_SERVER_URL", "http://gov.example.test")
        result = _run(_hook_input(tool_name, {"agent_id": "agent-x"}), tmp_path,
                      host="claude")
        assert "cleartext HTTP" in (_deny_reason(result) or "")

    def test_bundled_tool_outside_the_named_list_passes_on_secure_transport(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("UNITARES_SERVER_URL", "http://localhost:8767")
        _write_cache(tmp_path)
        result = _run(_hook_input(f"{BUNDLED}mark_response_complete",
                                  {"agent_id": "agent-x"}), tmp_path, host="claude")
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    @pytest.mark.parametrize("depth", [11, 12, 40])
    @pytest.mark.parametrize("key", ["kwargs", "arguments"])
    def test_proof_below_the_scan_depth_is_denied(self, tmp_path, monkeypatch, depth, key):
        # Nesting past the scan limit is not evidence of no proof: the body
        # still crosses the wire before the server rejects it.
        monkeypatch.setenv("UNITARES_SERVER_URL", "http://gov.example.test")
        nested: object = {"continuity_token": "v1.tok"}
        for _ in range(depth):
            nested = {key: nested}
        result = _run(_hook_input(f"{BUNDLED}use_tool", {
            "tool_name": "sync_state", "arguments": nested,
        }), tmp_path, host="claude")
        assert "cleartext HTTP" in (_deny_reason(result) or "")

    def test_proof_in_a_list_chain_below_the_scan_depth_is_denied(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNITARES_SERVER_URL", "http://gov.example.test")
        nested: object = {"client_session_id": SID}
        for _ in range(20):
            nested = [nested]
        result = _run(_hook_input(f"{BUNDLED}sync_state", {"kwargs": nested}),
                      tmp_path, host="claude")
        assert "cleartext HTTP" in (_deny_reason(result) or "")

    @pytest.mark.parametrize(
        "wrapped",
        [
            '{"continuity_token": "v1.tok"',  # truncated JSON
            "[" * 5000 + "]" * 5000,  # too deep for the JSON parser
            json.dumps(json.dumps({"client_session_id": SID})),  # double-encoded
        ],
        ids=["truncated-json", "too-deep-json", "double-encoded"],
    )
    def test_unparseable_string_container_with_proof_is_denied(
        self, tmp_path, monkeypatch, wrapped
    ):
        monkeypatch.setenv("UNITARES_SERVER_URL", "http://gov.example.test")
        if wrapped.startswith("["):
            wrapped = wrapped[:2500] + '{"agent_id": "a"}' + wrapped[2500:]
        result = _run(_hook_input(f"{BUNDLED}sync_state", {"kwargs": wrapped}),
                      tmp_path, host="claude")
        assert "cleartext HTTP" in (_deny_reason(result) or "")

    @pytest.mark.parametrize(
        "tool_input",
        [
            # JSON strings inside wrapper lists
            {"kwargs": [json.dumps({"client_session_id": SID})]},
            {"tool_name": "sync_state",
             "arguments": [json.dumps({"continuity_token": "v1.tok"})]},
            # JSON strings under keys that are not wrappers
            {"query": json.dumps({"client_session_id": SID})},
            {"payload": {"inner": [json.dumps([{"agent_id": "a"}])]}},
            # a JSON string inside a JSON string inside a list
            {"kwargs": [json.dumps(json.dumps({"agent_uuid": "u"}))]},
            # the field name only appears once the string is decoded
            {"note": '{"client\\u005fsession_id": "agent-x"}'},
            # proof under a key that is itself a JSON object
            {json.dumps({"continuity_token": "v1.tok"}): 1},
            # proof in a non-JSON string anywhere in the tree
            {"items": [["continuity_token=v1.tok"]]},
        ],
        ids=[
            "kwargs-list-json", "arguments-list-json", "plain-key-json",
            "nested-list-json", "double-encoded-in-list", "escaped-name",
            "json-key", "text-in-list",
        ],
    )
    def test_proof_anywhere_in_the_tree_is_denied(self, tmp_path, monkeypatch, tool_input):
        monkeypatch.setenv("UNITARES_SERVER_URL", "http://gov.example.test")
        result = _run(_hook_input(f"{BUNDLED}use_tool", tool_input), tmp_path,
                      host="claude")
        assert "cleartext HTTP" in (_deny_reason(result) or "")

    def test_tree_past_the_scan_budget_is_denied(self, tmp_path, monkeypatch):
        import governance_call_inject as inject

        monkeypatch.setenv("UNITARES_SERVER_URL", "http://gov.example.test")
        big = {"items": ["x" * 100] * (inject._PROOF_SCAN_BUDGET // 100 + 1)}
        assert inject._carries_proof(big) is True
        assert inject._carries_proof({"items": ["x" * 100] * 10}) is False

    def test_scan_error_counts_as_proof(self, monkeypatch, capsys):
        import io

        import governance_call_inject as inject

        def _boom(*_args, **_kwargs):
            raise RuntimeError("scan failed")

        monkeypatch.setenv("UNITARES_SERVER_URL", "http://gov.example.test")
        monkeypatch.setattr(inject, "_carries_proof", _boom)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_hook_input(
            f"{BUNDLED}search_shared_memory", {"query": "x"}))))
        assert inject.main(["--host", "claude"]) == 0
        specific = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
        assert specific["permissionDecision"] == "deny"

    @pytest.mark.parametrize(
        "tool_name",
        [
            f"{BUNDLED}sync_state__x",
            f"{BUNDLED}a__b__c",
            f"{BUNDLED}__sync_state",
            f"{BUNDLED.upper()}sync_state",
        ],
    )
    def test_bundled_prefix_decides_the_server(self, tmp_path, monkeypatch, tool_name):
        # Claude names tools mcp__<server>__<tool>; the tool part may itself
        # contain "__", so the server is the known prefix, not a split.
        matchers = _pre_governance_matchers("hooks/claude-hooks.json", "claude")
        if tool_name.startswith(BUNDLED):
            assert any(re.fullmatch(m, tool_name) for m in matchers), matchers
        monkeypatch.setenv("UNITARES_SERVER_URL", "http://gov.example.test")
        result = _run(_hook_input(tool_name, {"agent_id": "agent-x"}), tmp_path,
                      host="claude")
        assert "cleartext HTTP" in (_deny_reason(result) or "")

    @pytest.mark.parametrize(
        "server_url",
        ["http://127.1:8767", "http://0x7f.0.0.1:8767", "http://2130706433:8767"],
    )
    def test_loopback_shorthand_keeps_injection(self, tmp_path, monkeypatch, server_url):
        # Claude's URL parser resolves these to 127.0.0.1.
        monkeypatch.setenv("UNITARES_SERVER_URL", server_url)
        _write_cache(tmp_path)
        result = _run(_hook_input(f"{BUNDLED}sync_state", {}), tmp_path, host="claude")
        updated = _updated_input(result)
        assert updated is not None
        assert updated["client_session_id"] == SID

    def test_unreadable_payload_is_denied_whatever_it_names(self, tmp_path, monkeypatch):
        # A payload the hook cannot read cannot be attributed to a server, so
        # it is denied while the bundled transport is insecure.
        monkeypatch.setenv("UNITARES_SERVER_URL", "http://gov.example.test")
        payload = json.dumps(_hook_input("mcp__PLUGIN_x__sync_state", {
            "client_session_id": SID, "junk": "PLACEHOLDER",
        })).replace('"PLACEHOLDER"', "[" * _TOO_DEEP + "]" * _TOO_DEEP)
        result = subprocess.run(
            [str(HOOK), "--host", "claude"], input=payload, text=True,
            capture_output=True, timeout=10, cwd=str(tmp_path),
        )
        assert "cleartext HTTP" in (_deny_reason(result) or "")

    def test_payload_too_deep_for_the_hook_parser_is_denied(self, tmp_path, monkeypatch):
        # Proof sits at the top level, but unrelated nesting makes the whole
        # hook payload unparseable. That must not turn into a silent pass.
        monkeypatch.setenv("UNITARES_SERVER_URL", "http://gov.example.test")
        payload = json.dumps(_hook_input(f"{BUNDLED}sync_state", {
            "client_session_id": SID, "junk": "PLACEHOLDER",
        })).replace('"PLACEHOLDER"', "[" * _TOO_DEEP + "]" * _TOO_DEEP)
        result = subprocess.run(
            [str(HOOK), "--host", "claude"], input=payload, text=True,
            capture_output=True, timeout=10, cwd=str(tmp_path),
        )
        assert "cleartext HTTP" in (_deny_reason(result) or "")

    def test_unparseable_payload_on_secure_transport_still_passes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNITARES_SERVER_URL", "http://localhost:8767")
        payload = json.dumps(_hook_input(f"{BUNDLED}sync_state", {
            "junk": "PLACEHOLDER",
        })).replace('"PLACEHOLDER"', "[" * _TOO_DEEP + "]" * _TOO_DEEP)
        result = subprocess.run(
            [str(HOOK), "--host", "claude"], input=payload, text=True,
            capture_output=True, timeout=10, cwd=str(tmp_path),
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_proofless_call_passes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNITARES_SERVER_URL", "http://gov.example.test")
        result = _run(_hook_input(f"{BUNDLED}search_shared_memory", {
            "query": "x",
        }), tmp_path, host="claude")
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    @pytest.mark.parametrize(
        "server_url",
        [
            "http://gov.example.test\\@localhost:8767",
            "http://gov.example.test@localhost:8767",
        ],
    )
    def test_url_whose_host_parsers_disagree_on_is_insecure(
        self, tmp_path, monkeypatch, server_url
    ):
        # Claude's WHATWG parser connects to gov.example.test for the first
        # form; Python's urlsplit would name localhost.
        monkeypatch.setenv("UNITARES_SERVER_URL", server_url)
        result = _run(_hook_input(f"{BUNDLED}sync_state", {"client_session_id": SID}),
                      tmp_path, host="claude")
        assert "cleartext HTTP" in (_deny_reason(result) or "")

    @pytest.mark.parametrize(
        "server_url",
        [
            None,
            "",
            "https://gov.example.test",
            "http://localhost:8767",
            "http://127.0.0.1:8767",
            "http://[::1]:8767",
        ],
    )
    def test_secure_transport_keeps_injection(self, tmp_path, monkeypatch, server_url):
        if server_url is None:
            monkeypatch.delenv("UNITARES_SERVER_URL", raising=False)
        else:
            monkeypatch.setenv("UNITARES_SERVER_URL", server_url)
        _write_cache(tmp_path)
        result = _run(_hook_input(f"{BUNDLED}sync_state", {}), tmp_path, host="claude")
        updated = _updated_input(result)
        assert updated is not None
        assert updated["client_session_id"] == SID

    @pytest.mark.parametrize(
        "tool_name",
        ["mcp__unitares__sync_state", "mcp__unitares-governance__sync_state"],
    )
    def test_other_aliases_are_unchanged(self, tmp_path, monkeypatch, tool_name):
        monkeypatch.setenv("UNITARES_SERVER_URL", "http://gov.example.test")
        _write_cache(tmp_path)
        result = _run(_hook_input(tool_name, {}), tmp_path, host="claude")
        updated = _updated_input(result)
        assert updated is not None
        assert updated["client_session_id"] == SID

    def test_existing_deny_reason_is_kept(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNITARES_SERVER_URL", "http://gov.example.test")
        _write_cache(tmp_path)
        result = _run(_hook_input(f"{BUNDLED}sync_state", {
            "client_session_id": "agent-foreign-111",
        }), tmp_path, host="claude")
        assert "protected current-slot identity" in (_deny_reason(result) or "")


class TestFailOpen:

    def test_no_cache_no_output(self, tmp_path):
        result = _run(_hook_input(
            "mcp__unitares-governance__process_agent_update", {}), tmp_path)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_wrong_slot_no_output(self, tmp_path):
        # Slot-scoped read only: another session's cache must never leak in.
        _write_cache(tmp_path, slot="other-session-xyz")
        result = _run(_hook_input(
            "mcp__unitares-governance__process_agent_update", {}, slot=SLOT), tmp_path)
        assert result.stdout.strip() == ""

    def test_missing_session_id_no_output(self, tmp_path):
        _write_cache(tmp_path)
        hook_input = _hook_input("mcp__unitares-governance__process_agent_update", {})
        del hook_input["session_id"]
        result = _run(hook_input, tmp_path)
        assert result.stdout.strip() == ""

    def test_malformed_stdin_exits_zero(self, tmp_path):
        result = subprocess.run(
            [str(HOOK)], input="not json", text=True,
            capture_output=True, timeout=10, cwd=str(tmp_path),
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_cache_without_sid_no_output(self, tmp_path):
        cache_dir = tmp_path / ".unitares"
        cache_dir.mkdir(parents=True)
        (cache_dir / f"session-{SLOT}.json").write_text(json.dumps({"uuid": "u-1"}))
        result = _run(_hook_input(
            "mcp__unitares-governance__process_agent_update", {}), tmp_path)
        assert result.stdout.strip() == ""


# The plugin's lifecycle skill publishes the workflow-name table agents are
# taught to call. Deriving coverage from it (rather than from a second list
# kept in this file) means a workflow alias added to that table without
# injector and matcher coverage fails here instead of silently falling back
# to the displaceable onboard pin.
LIFECYCLE_SKILL = PLUGIN_ROOT / "skills" / "governance-lifecycle" / "SKILL.md"
_TOOL_CELL = re.compile(r"`([a-z][a-z0-9_]*)")


def _workflow_table_rows() -> list[tuple[str, str]]:
    """(workflow tool, raw implementation tool) pairs from the skill table."""
    text = LIFECYCLE_SKILL.read_text(encoding="utf-8")
    section = text.split("## Primary Workflow Names", 1)
    assert len(section) == 2, "Primary Workflow Names section missing from lifecycle skill"
    body = section[1].split("\n## ", 1)[0]
    rows = []
    for line in body.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 3:
            continue
        workflow = _TOOL_CELL.match(cells[1])
        raw = _TOOL_CELL.match(cells[2])
        if workflow and raw:
            rows.append((workflow.group(1), raw.group(1)))
    return rows


def _pre_governance_matchers(relative: str, host: str) -> list[str]:
    config = json.loads((PLUGIN_ROOT / relative).read_text(encoding="utf-8"))
    return [
        group["matcher"]
        for group in config["hooks"]["PreToolUse"]
        for handler in group["hooks"]
        if handler["command"].endswith(f" pre-governance-call --host {host}")
    ]


class TestWorkflowAliasCoverage:

    def test_skill_table_parses(self):
        rows = _workflow_table_rows()
        # Guard against a table-format change turning the checks below vacuous.
        assert len(rows) >= 6, rows
        assert ("sync_state", "process_agent_update") in rows

    def test_every_taught_workflow_tool_is_injectable(self):
        covered = INJECT_SUFFIXES | ANCHORED_MINT_SUFFIXES
        missing = sorted(
            name
            for row in _workflow_table_rows()
            for name in row
            if name not in covered
        )
        assert missing == [], (
            "workflow tools taught by the lifecycle skill but absent from "
            f"INJECT_SUFFIXES/ANCHORED_MINT_SUFFIXES: {missing}"
        )

    def test_aliases_of_tag_bearing_tools_are_tag_bearing(self):
        missing = sorted(
            workflow
            for workflow, raw in _workflow_table_rows()
            if raw in TAG_BEARING_SUFFIXES and workflow not in TAG_BEARING_SUFFIXES
        )
        assert missing == [], (
            f"aliases of a tag-bearing tool missing from TAG_BEARING_SUFFIXES: {missing}"
        )

    def test_server_initial_advertisement_is_injectable(self):
        # The server's initial progressive tools/list (default workflow order in
        # unitares src/mcp_handlers/introspection/tool_introspection.py) plus the
        # use_tool gateway. Coverage derived only from the skill table above
        # missed consult and use_tool, which the server advertises directly.
        advertised = {
            "start_session", "sync_state", "check_working_state",
            "search_shared_memory", "record_result", "consult", "request_review",
            "list_tools", "describe_tool", "store_finding", "self_recovery",
            "use_tool",
        }
        covered = INJECT_SUFFIXES | ANCHORED_MINT_SUFFIXES
        assert sorted(advertised - covered) == []

    def test_tag_bearing_tools_reach_the_normalizer(self):
        # main() returns before tag normalization for any suffix outside
        # INJECT_SUFFIXES, so a tag-bearing name there would be dead config.
        assert TAG_BEARING_SUFFIXES <= INJECT_SUFFIXES

    def test_codex_rewrite_scope_is_a_subset_of_injection(self):
        assert CODEX_REWRITE_SUFFIXES <= INJECT_SUFFIXES

    @pytest.mark.parametrize(
        "relative,host",
        [("hooks/claude-hooks.json", "claude"), ("hooks/codex-hooks.json", "codex")],
    )
    def test_hook_matchers_route_every_injectable_tool(self, relative, host):
        # The injector only runs for tools the host's PreToolUse matcher sends
        # to it; a suffix the matcher omits is never rewritten.
        matchers = _pre_governance_matchers(relative, host)
        assert matchers, f"{relative} has no pre-governance-call PreToolUse hook"
        unrouted = sorted(
            tool
            for tool in (
                INJECT_SUFFIXES
                | ANCHORED_MINT_SUFFIXES
                | IDENTITY_ASSERT_SUFFIXES
            )
            if not any(
                re.fullmatch(matcher, f"mcp__unitares-governance__{tool}")
                for matcher in matchers
            )
        )
        assert unrouted == [], f"{relative} matcher does not route: {unrouted}"


class TestDuplicateMintGuard:
    """A force_new mint in a slot that is already bound would give one process
    two identities (live 2026-10-14). The hook denies it unless the call
    declares lineage or a deliberate spawn_reason; it never rewrites it. A
    binding idle past the session lifetime is not this process's
    (tests/test_binding_activity.py)."""

    @staticmethod
    def _decision(result):
        out = json.loads(result.stdout)["hookSpecificOutput"]
        assert "updatedInput" not in out
        return out

    @pytest.mark.parametrize("tool", ["start_session", "onboard"])
    @pytest.mark.parametrize("extra", [{}, {"spawn_reason": "new_session"}])
    def test_denies_second_mint_in_bound_slot(self, tmp_path, tool, extra):
        _write_cache(tmp_path)
        result = _run(_hook_input(
            f"mcp__unitares-governance__{tool}", {"force_new": True, **extra}), tmp_path)
        out = self._decision(result)
        assert out["permissionDecision"] == "deny"
        assert "already bound" in out["permissionDecisionReason"]
        assert SID in out["permissionDecisionReason"]
        assert 'spawn_reason="explicit"' in out["permissionDecisionReason"]

    @pytest.mark.parametrize("extra", [
        {"parent_agent_id": "11111111-2222-3333-4444-555555555555"},
        {"spawn_reason": "explicit"},
        {"spawn_reason": "subagent"},
        {"spawn_reason": "compaction"},
    ])
    def test_declared_lineage_or_deliberate_spawn_passes(self, tmp_path, extra):
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__start_session", {"force_new": True, **extra}), tmp_path)
        assert result.stdout.strip() == ""

    def test_unbound_slot_mints_freely(self, tmp_path):
        result = _run(_hook_input(
            "mcp__unitares-governance__start_session", {"force_new": True}), tmp_path)
        assert result.stdout.strip() == ""

    def test_without_force_new_nothing_is_denied(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input("mcp__unitares-governance__start_session", {}), tmp_path)
        assert result.stdout.strip() == ""

    def test_foreign_proof_field_is_denied(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__start_session",
            {"force_new": True, "client_session_id": "agent-other"}), tmp_path)
        assert self._decision(result)["permissionDecision"] == "deny"

    def test_codex_host_denies_too(self, tmp_path):
        _write_cache(tmp_path)
        result = _run(_hook_input(
            "mcp__unitares-governance__start_session", {"force_new": True}), tmp_path, host="codex")
        assert self._decision(result)["permissionDecision"] == "deny"

    @pytest.mark.parametrize(
        "tool_name",
        [
            "mcp__unitares-governance__identity",
            "mcp__unitares_governance__identity",
        ],
    )
    @pytest.mark.parametrize("tool_input", [{}, {"force_new": False}])
    def test_denies_proofless_identity_in_bound_slot(
        self, tmp_path, tool_name, tool_input
    ):
        _write_cache(tmp_path)
        result = _run(_hook_input(tool_name, tool_input), tmp_path, host="codex")
        out = self._decision(result)
        assert out["permissionDecision"] == "deny"
        assert "Bare identity()" in out["permissionDecisionReason"]
        assert SID in out["permissionDecisionReason"]
        assert "identity(client_session_id=...)" in out["permissionDecisionReason"]

    @pytest.mark.parametrize(
        "proof",
        [
            {"client_session_id": "agent-explicit"},
            {"continuity_token": "v1.explicit"},
            {"agent_uuid": "11111111-2222-3333-4444-555555555555"},
            {"agent_id": "11111111-2222-3333-4444-555555555555"},
            {"name": "cosmetic-name"},
        ],
    )
    def test_identity_with_explicit_signal_passes(self, tmp_path, proof):
        _write_cache(tmp_path)
        result = _run(
            _hook_input("mcp__unitares-governance__identity", proof),
            tmp_path,
            host="codex",
        )
        assert result.stdout.strip() == ""

    def test_proofless_identity_in_unbound_slot_passes(self, tmp_path):
        result = _run(
            _hook_input("mcp__unitares-governance__identity", {}),
            tmp_path,
            host="codex",
        )
        assert result.stdout.strip() == ""

    def test_proofless_identity_in_inherited_slot_passes(self, tmp_path):
        cache = tmp_path / ".unitares" / f"session-{SLOT}.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({
            "uuid": "cafe1234-0000-0000-0000-000000000000",
            "client_session_id": SID,
            "prior_process": True,
            "slot": SLOT,
        }))
        result = _run(
            _hook_input("mcp__unitares-governance__identity", {}),
            tmp_path,
            host="codex",
        )
        assert result.stdout.strip() == ""


def test_cache_without_a_binding_does_not_deny(tmp_path):
    cache = tmp_path / ".unitares" / f"session-{SLOT}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"uuid": "cafe1234-0000-0000-0000-000000000000"}))
    result = _run(_hook_input(
        "mcp__unitares-governance__start_session", {"force_new": True}), tmp_path)
    assert result.stdout.strip() == ""


def test_cache_inherited_by_a_resumed_process_does_not_deny(tmp_path):
    cache = tmp_path / ".unitares" / f"session-{SLOT}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"uuid": "cafe1234-0000-0000-0000-000000000000",
                                 "client_session_id": SID, "prior_process": True}))
    result = _run(_hook_input(
        "mcp__unitares-governance__start_session", {"force_new": True}), tmp_path)
    assert result.stdout.strip() == ""
