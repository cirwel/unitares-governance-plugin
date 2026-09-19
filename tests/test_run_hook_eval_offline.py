from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "hooks" / "run-hook.cmd"
ENDPOINT_VARS = (
    "UNITARES_SERVER_URL",
    "UNITARES_LEASE_PLANE_URL",
    "UNITARES_SIDECAR_URL",
    "UNITARES_AUTO_ONBOARD",
    "UNITARES_DISABLE_AUTO_ONBOARD",
)


def _run_env_probe(tmp_path: Path, extra_env: dict[str, str]) -> dict[str, str]:
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy(WRAPPER, hooks / "run-hook.cmd")
    probe = hooks / "env-probe"
    probe.write_text(
        "#!/bin/bash\n"
        + "".join(f'echo "{name}=${{{name}:-}}"\n' for name in ENDPOINT_VARS)
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("UNITARES_", "EVAL_"))}
    env.update(extra_env)
    result = subprocess.run(
        ["sh", str(hooks / "run-hook.cmd"), "env-probe"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines())


def test_eval_offline_redirects_every_endpoint_and_disables_onboard(tmp_path: Path) -> None:
    seen = _run_env_probe(
        tmp_path,
        {"EVAL_UNITARES_OFFLINE": "1", "UNITARES_SERVER_URL": "http://localhost:8767"},
    )

    assert seen["UNITARES_SERVER_URL"] == "http://127.0.0.1:9"
    assert seen["UNITARES_LEASE_PLANE_URL"] == "http://127.0.0.1:9"
    assert seen["UNITARES_SIDECAR_URL"] == "http://127.0.0.1:9"
    assert seen["UNITARES_AUTO_ONBOARD"] == "off"
    assert seen["UNITARES_DISABLE_AUTO_ONBOARD"] == "1"


def test_without_eval_flag_environment_passes_through(tmp_path: Path) -> None:
    seen = _run_env_probe(tmp_path, {"UNITARES_SERVER_URL": "http://localhost:8767"})

    assert seen["UNITARES_SERVER_URL"] == "http://localhost:8767"
    assert seen["UNITARES_AUTO_ONBOARD"] == ""
    assert seen["UNITARES_DISABLE_AUTO_ONBOARD"] == ""


def _plugin_copy(tmp_path: Path) -> Path:
    root = tmp_path / "plugin"
    for name in ("hooks", "config", "scripts"):
        shutil.copytree(ROOT / name, root / name)
    return root


def _resolve_through_real_defaults(tmp_path: Path, extra_env: dict[str, str]) -> dict[str, str]:
    """Run the wrapper on a probe that sources config/defaults.env the way the
    hooks do, then ask the Python helpers what they would actually use."""
    root = _plugin_copy(tmp_path)
    probe = root / "hooks" / "resolve-probe"
    probe.write_text(
        "#!/bin/bash\n"
        'ROOT="$(cd "$(dirname "$0")/.." && pwd)"\n'
        'SERVER_URL_OVERRIDE="${UNITARES_SERVER_URL-}"\n'
        'source "$ROOT/config/defaults.env"\n'
        '[[ -n "$SERVER_URL_OVERRIDE" ]] && UNITARES_SERVER_URL="$SERVER_URL_OVERRIDE"\n'
        'cd "$ROOT/scripts" && python3 -c \''
        "import os, checkin, file_lease_hook as f; "
        'print(f"lease_url={f._base_url()}"); '
        'print(f"leases_enabled={f._enabled()}"); '
        'print(f"checkins_killed={checkin._is_killed()}"); '
        "print(\"server_url=\" + os.environ[\"UNITARES_SERVER_URL\"])'\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("UNITARES_", "EVAL_", "LEASE_PLANE_"))}
    env["HOME"] = str(tmp_path / "home")
    env.update(extra_env)
    result = subprocess.run(
        ["sh", str(root / "hooks" / "run-hook.cmd"), "resolve-probe"],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def test_eval_offline_survives_defaults_env_and_reaches_the_helpers(tmp_path: Path) -> None:
    seen = _resolve_through_real_defaults(tmp_path, {"EVAL_UNITARES_OFFLINE": "1"})

    assert seen["lease_url"] == "http://127.0.0.1:9"
    assert seen["leases_enabled"] == "False"
    assert seen["checkins_killed"] == "True"
    assert seen["server_url"] == "http://127.0.0.1:9"


def test_without_eval_flag_helpers_use_the_live_defaults(tmp_path: Path) -> None:
    seen = _resolve_through_real_defaults(tmp_path, {})

    assert seen["lease_url"] == "http://127.0.0.1:8788"
    assert seen["leases_enabled"] == "True"
    assert seen["checkins_killed"] == "False"


def test_windows_branch_sets_the_same_offline_values() -> None:
    text = WRAPPER.read_text()
    cmd_half = text.split("CMDBLOCK", 2)[1]
    unix_half = text.split("CMDBLOCK", 2)[2]
    gate = unix_half.split('if [ "${EVAL_UNITARES_OFFLINE:-}" = "1" ]; then', 1)[1].split("fi", 1)[0]
    unix_values = dict(
        line.strip().split("=", 1)
        for line in gate.splitlines()
        if "=" in line and not line.strip().startswith(("export", "#"))
    )

    assert '"%EVAL_UNITARES_OFFLINE%"=="1"' in cmd_half
    for name, value in unix_values.items():
        assert f'set "{name}={value.strip(chr(34))}"' in cmd_half, name
    # The gate must run before any branch hands off to bash.
    assert cmd_half.index("EVAL_UNITARES_OFFLINE") < cmd_half.index("bash.exe")


def test_every_eval_case_sets_the_offline_flag() -> None:
    cases = sorted((ROOT / "evals").glob("**/prompt.md")) + sorted((ROOT / "evals").glob("**/case.yaml"))
    cases = [c for c in cases if "results" not in c.parts and "mocks" not in c.parts]

    assert cases
    for case in cases:
        assert 'EVAL_UNITARES_OFFLINE: "1"' in case.read_text(), case.relative_to(ROOT)


def test_every_claude_hook_goes_through_the_dispatcher() -> None:
    config = json.loads((ROOT / "hooks" / "claude-hooks.json").read_text())
    commands = [
        hook["command"]
        for entries in config["hooks"].values()
        for entry in entries
        for hook in entry["hooks"]
    ]

    assert commands
    for command in commands:
        assert command.startswith('"${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd" '), command
