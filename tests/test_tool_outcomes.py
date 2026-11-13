"""Tool-outcome counting from the Claude transcript, by file position.

Transcripts are built entry by entry in the shape the host writes: assistant
entries carry ``tool_use`` blocks, user entries carry ``tool_result`` blocks,
and a user entry with anything else opens a turn.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import tool_outcomes  # noqa: E402
from tool_outcomes import collect_window, run  # noqa: E402


def _prompt(text: str = "do the thing") -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _call(call_id: str, name: str = "Bash", tool_input: dict | None = None) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": call_id, "name": name,
                 "input": tool_input if tool_input is not None else {"command": "ls"}}
            ],
        },
    }


def _result(call_id: str, *, error: bool = False, text: str = "Exit code 1\nsecret") -> dict:
    block = {"type": "tool_result", "tool_use_id": call_id, "content": text}
    if error:
        block["is_error"] = True
    return {"type": "user", "message": {"role": "user", "content": [block]}}


class Transcript:
    def __init__(self, tmp_path: Path, name: str = "t.jsonl"):
        self.path = tmp_path / name
        self.path.write_text("")

    def add(self, *entries: dict) -> "Transcript":
        with self.path.open("a") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")
        return self

    def __str__(self) -> str:
        return str(self.path)


def _counts(counts: dict) -> tuple[int, int, int, int]:
    return (
        counts["tool_delta"],
        counts["tool_error_delta"],
        counts["tool_not_executed_delta"],
        counts["repeat_failure_delta"],
    )


def test_first_sight_counts_only_the_closing_turn(tmp_path):
    t = Transcript(tmp_path).add(
        _prompt("earlier"), _call("a"), _result("a", error=True),
        _prompt("now"), _call("b"), _result("b"), _call("c"), _result("c"),
    )

    counts, _state = collect_window(str(t), None)

    assert _counts(counts) == (2, 0, 0, 0)
    assert counts["window_truncated"] is False
    assert counts["window_basis"] == "transcript_cursor"


def test_a_second_stop_for_the_same_prompt_counts_nothing_twice(tmp_path):
    t = Transcript(tmp_path).add(_prompt(), _call("a"), _result("a", error=True))
    counts, state = collect_window(str(t), None)
    assert _counts(counts) == (1, 1, 0, 0)

    # A blocked Stop: the turn continues with no new prompt, then stops again.
    t.add(_call("b"), _result("b"))
    counts, state = collect_window(str(t), state)
    assert _counts(counts) == (1, 0, 0, 0)

    counts, _state = collect_window(str(t), state)
    assert _counts(counts) == (0, 0, 0, 0)


def test_an_interrupted_turn_is_counted_by_the_next_stop(tmp_path):
    t = Transcript(tmp_path).add(_prompt(), _call("a"), _result("a"))
    _counts_first, state = collect_window(str(t), None)

    # The next turn fails twice and is interrupted: no Stop fires. Then a new
    # prompt runs to completion and its Stop does fire.
    same = {"command": "make test"}
    t.add(
        _prompt("second"),
        _call("b", tool_input=same), _result("b", error=True),
        _call("c", tool_input=same), _result("c", error=True),
        _prompt("third"), _call("d"), _result("d"),
    )

    counts, _state = collect_window(str(t), state)

    assert _counts(counts) == (3, 2, 0, 1)


def test_a_new_prompt_arriving_first_does_not_empty_the_window(tmp_path):
    t = Transcript(tmp_path).add(_prompt(), _call("a"), _result("a"))
    _first, state = collect_window(str(t), None)
    t.add(_call("b"), _result("b", error=True), _prompt("already here"))

    counts, _state = collect_window(str(t), state)

    assert _counts(counts) == (1, 1, 0, 0)


def test_repeats_are_recognized_across_windows_and_cleared_by_success(tmp_path):
    same = {"command": "make test"}
    t = Transcript(tmp_path).add(
        _prompt(), _call("a", tool_input=same), _result("a", error=True)
    )
    _first, state = collect_window(str(t), None)

    t.add(_prompt("again"), _call("b", tool_input=same), _result("b", error=True))
    counts, state = collect_window(str(t), state)
    assert _counts(counts) == (1, 1, 0, 1)

    # It works, so a later failure of the same call starts over.
    t.add(_prompt("fixed"), _call("c", tool_input=same), _result("c"))
    counts, state = collect_window(str(t), state)
    assert _counts(counts) == (1, 0, 0, 0)

    t.add(_prompt("broke"), _call("d", tool_input=same), _result("d", error=True))
    counts, _state = collect_window(str(t), state)
    assert _counts(counts) == (1, 1, 0, 0)


def test_a_reworded_bash_description_does_not_hide_a_repeat(tmp_path):
    t = Transcript(tmp_path).add(
        _prompt(),
        _call("a", tool_input={"command": "make test", "description": "Run tests"}),
        _result("a", error=True),
        _call("b", tool_input={"command": "make test", "description": "Retry tests",
                               "timeout": 600000}),
        _result("b", error=True),
        _call("c", tool_input={"command": "make lint", "description": "Run tests"}),
        _result("c", error=True),
    )

    counts, _state = collect_window(str(t), None)

    assert _counts(counts) == (3, 3, 0, 1)


def test_description_still_distinguishes_calls_on_other_tools(tmp_path):
    t = Transcript(tmp_path).add(
        _prompt(),
        _call("a", name="Agent", tool_input={"prompt": "x", "description": "one"}),
        _result("a", error=True),
        _call("b", name="Agent", tool_input={"prompt": "x", "description": "two"}),
        _result("b", error=True),
    )

    counts, _state = collect_window(str(t), None)

    assert counts["repeat_failure_delta"] == 0


@pytest.mark.parametrize("text", [
    "PreToolUse:Bash hook error: [command hook 3]: BLOCKED: blanket add",
    "The user doesn't want to proceed with this tool use. The tool use was rejected.",
    "[Request interrupted by user for tool use]",
    "Permission to use Bash has been denied.",
])
def test_calls_that_never_ran_are_counted_apart_and_are_not_repeats(tmp_path, text):
    same = {"command": "git add -A"}
    t = Transcript(tmp_path).add(
        _prompt(),
        _call("a", tool_input=same), _result("a", error=True, text=text),
        _call("b", tool_input=same), _result("b", error=True, text=text),
    )

    counts, _state = collect_window(str(t), None)

    assert _counts(counts) == (2, 2, 2, 0)


def test_result_text_given_as_blocks_is_read_the_same_way(tmp_path):
    entry = _result("a", error=True)
    entry["message"]["content"][0]["content"] = [
        {"type": "text", "text": "[Request interrupted by user for tool use]"}
    ]
    t = Transcript(tmp_path).add(_prompt(), _call("a"), entry)

    counts, _state = collect_window(str(t), None)

    assert _counts(counts) == (1, 1, 1, 0)


def test_a_result_whose_call_was_in_an_earlier_window_still_pairs(tmp_path):
    same = {"command": "make test"}
    t = Transcript(tmp_path).add(
        _prompt(), _call("a", tool_input=same), _result("a", error=True),
        _call("b", tool_input=same),  # still running when the Stop fires
    )
    counts, state = collect_window(str(t), None)
    assert _counts(counts) == (1, 1, 0, 0)

    t.add(_result("b", error=True))
    counts, _state = collect_window(str(t), state)
    assert _counts(counts) == (1, 1, 0, 1)


def test_duplicated_entries_other_types_and_sidechains_are_ignored(tmp_path):
    blob = dict(_call("x"), type="api-request-blob")
    side_call = dict(_call("s"), isSidechain=True)
    side_result = dict(_result("s", error=True), isSidechain=True)
    t = Transcript(tmp_path).add(
        _prompt(), _call("a"), _call("a"), _result("a", error=True), _result("a", error=True),
        blob, side_call, side_result, {"type": "attachment"},
    )

    counts, _state = collect_window(str(t), None)

    assert _counts(counts) == (1, 1, 0, 0)


def test_a_half_written_last_line_waits_for_the_next_call(tmp_path):
    t = Transcript(tmp_path).add(_prompt(), _call("a"), _result("a"))
    with t.path.open("a") as handle:
        handle.write(json.dumps(_call("b"))[:20])  # no newline yet
    counts, state = collect_window(str(t), None)
    assert _counts(counts) == (1, 0, 0, 0)

    with t.path.open("a") as handle:
        handle.write(json.dumps(_call("b"))[20:] + "\n")
    t.add(_result("b", error=True))
    counts, _state = collect_window(str(t), state)
    assert _counts(counts) == (1, 1, 0, 0)


def test_more_unread_than_the_bound_is_a_truncated_lower_bound(tmp_path):
    t = Transcript(tmp_path).add(_prompt(), _call("a"), _result("a"))
    _first, state = collect_window(str(t), None)
    for index in range(60):
        t.add(_call(f"c{index}"), _result(f"c{index}", error=True, text="Exit code 1"))

    counts, state = collect_window(str(t), state, read_bound=3000)

    assert counts["window_truncated"] is True
    assert 0 < counts["tool_delta"] < 60
    assert counts["tool_error_delta"] <= counts["tool_delta"]
    assert state["offset"] == t.path.stat().st_size

    counts, _state = collect_window(str(t), state, read_bound=3000)
    assert _counts(counts) == (0, 0, 0, 0) and counts["window_truncated"] is False


def test_a_replaced_or_shrunken_transcript_starts_over(tmp_path):
    t = Transcript(tmp_path).add(_prompt(), _call("a"), _result("a", error=True))
    _first, state = collect_window(str(t), None)

    other = Transcript(tmp_path, "other.jsonl").add(
        _prompt("old"), _call("x"), _result("x", error=True),
        _prompt("new"), _call("y"), _result("y"),
    )
    counts, _state = collect_window(str(other), state)
    assert _counts(counts) == (1, 0, 0, 0)  # only the closing turn of the new file

    shrunk = dict(state, offset=10_000_000)
    counts, _state = collect_window(str(t), shrunk)
    assert counts["window_truncated"] is True


def test_counts_always_satisfy_the_server_bounds(tmp_path):
    same = {"command": "x"}
    t = Transcript(tmp_path).add(_prompt())
    for index in range(30):
        text = "[Request interrupted by user for tool use]" if index % 3 == 0 else "Exit code 2"
        t.add(_call(f"c{index}", tool_input=same), _result(f"c{index}", error=index % 2 == 0, text=text))

    counts, _state = collect_window(str(t), None)

    assert counts["tool_error_delta"] <= counts["tool_delta"]
    assert counts["tool_not_executed_delta"] <= counts["tool_error_delta"]
    assert counts["repeat_failure_delta"] <= (
        counts["tool_error_delta"] - counts["tool_not_executed_delta"]
    )


def test_unreadable_or_irregular_paths_return_none(tmp_path):
    assert collect_window(None, None) is None
    assert collect_window("relative.jsonl", None) is None
    assert collect_window(str(tmp_path / "missing.jsonl"), None) is None
    assert collect_window(str(tmp_path / "wrong.txt"), None) is None
    fifo = tmp_path / "pipe.jsonl"
    os.mkfifo(fifo)
    assert collect_window(str(fifo), None) is None
    target = Transcript(tmp_path, "real.jsonl").add(_prompt())
    link = tmp_path / "link.jsonl"
    link.symlink_to(target.path)
    assert collect_window(str(link), None) is None


def _run(t, tmp_path, sent, **overrides):
    kwargs = dict(
        agent_uuid="6bccb016-774c-44cf-90a0-612d2117f607",
        client_session_id="agent-test-session",
        slot="slot-1",
        server_url="http://localhost:9",
        home=tmp_path,
        sender=lambda url, body: sent.append((url, body)) or True,
    )
    kwargs.update(overrides)
    return run(json.dumps({"transcript_path": str(t), "session_id": "slot-1"}), **kwargs)


def test_run_posts_counts_and_nothing_else(tmp_path, monkeypatch):
    monkeypatch.delenv("UNITARES_TOOL_OUTCOMES", raising=False)
    monkeypatch.delenv("UNITARES_CHECKINS", raising=False)
    t = Transcript(tmp_path).add(
        _prompt("private prompt"),
        _call("a", tool_input={"command": "cat ~/.ssh/id_rsa"}),
        _result("a", error=True, text="Exit code 1\nprivate output"),
    )
    sent: list = []

    assert _run(t, tmp_path, sent) == "sent"

    (url, body), = sent
    assert url == "http://localhost:9"
    assert body["observation_kind"] == "tool_outcome_rollup"
    assert body["host_family"] == "claude"
    assert (body["tool_delta"], body["tool_error_delta"]) == (1, 1)
    assert body["window_truncated"] is False
    wire = json.dumps(body)
    for private in ("id_rsa", "private", "Bash", "Exit code", "slot-1"):
        assert private not in wire
    ledger = "".join(p.read_text() for p in (tmp_path / ".unitares").rglob("*.json"))
    for private in ("id_rsa", "private prompt", "private output", "Exit code"):
        assert private not in ledger


def test_run_is_at_most_once_and_idempotent_per_window(tmp_path, monkeypatch):
    monkeypatch.delenv("UNITARES_TOOL_OUTCOMES", raising=False)
    monkeypatch.delenv("UNITARES_CHECKINS", raising=False)
    t = Transcript(tmp_path).add(_prompt(), _call("a"), _result("a"))
    sent: list = []

    assert _run(t, tmp_path, sent, sender=lambda url, body: False) == "post_failed"
    # The window that failed to post is not resent, so it cannot double count.
    assert _run(t, tmp_path, sent) == "skip_nothing_new"
    assert sent == []

    t.add(_call("b"), _result("b"))
    assert _run(t, tmp_path, sent) == "sent"
    assert sent[0][1]["tool_delta"] == 1


def test_run_skips_when_unbound_disabled_or_without_a_transcript(tmp_path, monkeypatch):
    monkeypatch.delenv("UNITARES_CHECKINS", raising=False)
    t = Transcript(tmp_path).add(_prompt(), _call("a"), _result("a"))
    sent: list = []

    monkeypatch.delenv("UNITARES_TOOL_OUTCOMES", raising=False)
    assert _run(t, tmp_path, sent, client_session_id="") == "skip_unbound"
    assert _run(tmp_path / "missing.jsonl", tmp_path, sent) == "skip_no_transcript"
    monkeypatch.setenv("UNITARES_TOOL_OUTCOMES", "off")
    assert _run(t, tmp_path, sent) == "skip_disabled"
    monkeypatch.delenv("UNITARES_TOOL_OUTCOMES")
    monkeypatch.setenv("UNITARES_CHECKINS", "off")
    assert _run(t, tmp_path, sent) == "skip_disabled"
    assert sent == []
    # An unbound or disabled Stop leaves no cursor behind.
    assert not list((tmp_path / ".unitares").rglob("outcomes-*.json"))


def test_event_id_is_stable_for_a_window_and_differs_between_windows(tmp_path):
    state = {"inode": 7, "window_start": 0, "offset": 100}
    counts = {
        "tool_delta": 1, "tool_error_delta": 0, "tool_not_executed_delta": 0,
        "repeat_failure_delta": 0, "window_truncated": False,
        "window_basis": "transcript_cursor",
    }
    kwargs = dict(agent_uuid="u", client_session_id="c", slot="s", plugin_version="1")

    first = tool_outcomes.build_payload(counts, state, **kwargs)
    again = tool_outcomes.build_payload(counts, state, now=1.0, **kwargs)
    later = tool_outcomes.build_payload(counts, dict(state, window_start=100, offset=200), **kwargs)

    assert first["event_id"] == again["event_id"]
    assert first["event_id"] != later["event_id"]
