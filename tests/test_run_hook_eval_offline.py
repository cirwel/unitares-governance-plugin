from __future__ import annotations

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
