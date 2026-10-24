#!/usr/bin/env python3
"""Build a hook-free portable UNITARES plugin for ChatGPT.

The source repository remains the Claude/Codex host package. This builder
materializes a separate portable Agent Plugins package that either references
an MCP app already registered in ChatGPT or connects directly to a hosted
UNITARES MCP endpoint with OAuth Dynamic Client Registration.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit
import zipfile


ROOT = Path(__file__).resolve().parents[2]
PORTABLE_PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
PORTABLE_MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
APP_ID_RE = re.compile(
    r"^(?:plugin_asdk_app|asdk_app|connector|templated_apps)_[A-Za-z0-9_-]+$"
)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _validate_app_id(value: str) -> str:
    if not APP_ID_RE.fullmatch(value):
        raise ValueError(
            "app id must be a registered ChatGPT app id beginning with "
            "plugin_asdk_app_, asdk_app_, connector_, or templated_apps_"
        )
    return value


def _validate_server_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("server URL must be an absolute https:// URL")
    if parsed.username or parsed.password:
        raise ValueError("server URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("server URL must not contain a query or fragment")
    if not parsed.path.rstrip("/").endswith("/mcp"):
        raise ValueError("server URL path must end in /mcp or /mcp/")
    return value


def _plugin_manifest(*, app_reference: bool) -> dict:
    source = _read_json(ROOT / ".codex-plugin" / "plugin.json")
    openai_extension = {
        "onboardingSkill": "./skills/setup-unitares/SKILL.md",
        "interface": {
            "displayName": "UNITARES Governance",
            "shortDescription": "Agent identity, review, outcomes, and shared memory.",
            "longDescription": (
                "Connect ChatGPT to one operator-controlled UNITARES server for "
                "attributed agent identity, shared findings, governed review, "
                "outcome records, and reconstruction across sessions."
            ),
            "developerName": "CIRWEL",
            "category": "Productivity",
            "capabilities": ["Interactive", "Write"],
            "websiteURL": source["homepage"],
            "defaultPrompt": [
                "Set up UNITARES for this conversation and verify the connection.",
                "Search UNITARES shared memory before I continue this work.",
                "Record this milestone in UNITARES and report the governance action.",
            ],
            "brandColor": "#2F6B5F",
        },
    }
    if app_reference:
        openai_extension["apps"] = "./.app.json"

    return {
        "$schema": PORTABLE_PLUGIN_SCHEMA,
        "name": source["name"],
        "version": source["version"],
        "description": source["description"],
        "author": source["author"],
        "homepage": source["homepage"],
        "repository": source["repository"],
        "license": source["license"],
        "keywords": source["keywords"],
        "extensions": {"com.openai": openai_extension},
    }


def _copy_skills(output: Path) -> None:
    destination = output / "skills"
    destination.mkdir()
    for skill_dir in sorted((ROOT / "skills").iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_dir.is_dir() or not skill_file.is_file():
            continue
        target = destination / skill_dir.name
        target.mkdir()
        shutil.copy2(skill_file, target / "SKILL.md")

    setup_source = ROOT / "packaging" / "chatgpt" / "skills" / "setup-unitares"
    shutil.copytree(setup_source, destination / "setup-unitares")


def _write_registered_app(output: Path, app_id: str) -> None:
    _write_json(
        output / ".app.json",
        {
            "apps": {
                "unitares-governance": {
                    "id": _validate_app_id(app_id),
                    "required": True,
                }
            }
        },
    )


def _write_remote_server(output: Path, server_url: str) -> None:
    _write_json(
        output / "mcp.json",
        {
            "$schema": PORTABLE_MCP_SCHEMA,
            "mcpServers": {
                "unitares-governance": {
                    "type": "streamable-http",
                    "url": _validate_server_url(server_url),
                }
            },
        },
    )


def _write_archive(source: Path, archive: Path) -> None:
    if archive.exists():
        raise ValueError(f"archive already exists: {archive}")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            info = zipfile.ZipInfo(path.relative_to(source).as_posix())
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            bundle.writestr(info, path.read_bytes())


def build(
    output: Path,
    *,
    app_id: str | None,
    server_url: str | None,
    archive: Path | None,
) -> None:
    output = output.resolve()
    archive = archive.resolve() if archive is not None else None
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    if archive is not None and archive.exists():
        raise ValueError(f"archive already exists: {archive}")
    if archive is not None and archive.is_relative_to(output):
        raise ValueError("archive must be outside the generated package directory")
    if (app_id is None) == (server_url is None):
        raise ValueError("choose exactly one of app_id or server_url")

    # Validate before creating output so bad input leaves no partial package.
    if app_id is not None:
        _validate_app_id(app_id)
    if server_url is not None:
        _validate_server_url(server_url)

    output.mkdir(parents=True)
    _write_json(output / "plugin.json", _plugin_manifest(app_reference=app_id is not None))
    _copy_skills(output)
    shutil.copy2(ROOT / "LICENSE", output / "LICENSE")
    shutil.copy2(ROOT / "NOTICE", output / "NOTICE")

    if app_id is not None:
        _write_registered_app(output, app_id)
    else:
        assert server_url is not None
        _write_remote_server(output, server_url)

    if archive is not None:
        _write_archive(output, archive)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    connection = parser.add_mutually_exclusive_group(required=True)
    connection.add_argument(
        "--app-id",
        help="registered ChatGPT MCP app id, such as plugin_asdk_app_...",
    )
    connection.add_argument(
        "--server-url",
        help="hosted UNITARES streamable-HTTP URL ending in /mcp",
    )
    parser.add_argument("--output", type=Path, required=True, help="new package directory")
    parser.add_argument("--archive", type=Path, help="optional new ZIP path")
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    try:
        build(
            args.output.resolve(),
            app_id=args.app_id,
            server_url=args.server_url,
            archive=args.archive.resolve() if args.archive else None,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(f"built ChatGPT plugin package: {args.output.resolve()}")
    if args.archive:
        print(f"built upload archive: {args.archive.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
