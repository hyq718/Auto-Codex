#!/usr/bin/env python3
"""Install Auto-Codex into local Codex skill/plugin discovery paths."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path


PLUGIN_NAME = "auto-codex"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def replace_path(target: Path, source: Path, copy_mode: bool) -> None:
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)

    ensure_parent(target)
    if copy_mode:
        shutil.copytree(source, target, symlinks=True)
    else:
        target.symlink_to(source)


def install_skill(source: Path, target: Path, copy_mode: bool) -> str:
    replace_path(target, source, copy_mode)
    mode = "copied" if copy_mode else "symlinked"
    return f"{mode} skill -> {target}"


def default_marketplace() -> dict:
    return {
        "name": "auto-codex-local",
        "interface": {"displayName": "Auto-Codex Local"},
        "plugins": [],
    }


def ensure_plugin_entry(marketplace: dict, plugin_path: str) -> None:
    plugins = marketplace.setdefault("plugins", [])
    for entry in plugins:
        if entry.get("name") == PLUGIN_NAME:
            entry["source"] = {"source": "local", "path": plugin_path}
            entry["policy"] = {
                "installation": "AVAILABLE",
                "authentication": "ON_INSTALL",
            }
            entry["category"] = "Productivity"
            return

    plugins.append(
        {
            "name": PLUGIN_NAME,
            "source": {"source": "local", "path": plugin_path},
            "policy": {
                "installation": "AVAILABLE",
                "authentication": "ON_INSTALL",
            },
            "category": "Productivity",
        }
    )


def install_plugin(source: Path, plugins_root: Path, marketplace_path: Path, copy_mode: bool) -> list[str]:
    results: list[str] = []
    target = plugins_root / PLUGIN_NAME
    replace_path(target, source, copy_mode)
    mode = "copied" if copy_mode else "symlinked"
    results.append(f"{mode} plugin -> {target}")

    marketplace: dict
    if marketplace_path.exists():
        marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
    else:
        marketplace = default_marketplace()

    marketplace.setdefault("name", "auto-codex-local")
    marketplace.setdefault("interface", {}).setdefault("displayName", "Auto-Codex Local")
    ensure_plugin_entry(marketplace, f"./plugins/{PLUGIN_NAME}")
    ensure_parent(marketplace_path)
    marketplace_path.write_text(json.dumps(marketplace, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    results.append(f"updated marketplace -> {marketplace_path}")
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install Auto-Codex for local Codex usage.")
    parser.add_argument(
        "--home",
        default=str(Path.home()),
        help="Home directory used to resolve ~/.agents, ~/.codex, and ~/plugins targets.",
    )
    parser.add_argument("--copy", action="store_true", help="Copy files instead of creating symlinks.")
    parser.add_argument("--no-agents-skill", action="store_true", help="Skip installation into ~/.agents/skills.")
    parser.add_argument("--no-codex-skill", action="store_true", help="Skip installation into ~/.codex/skills.")
    parser.add_argument("--no-plugin", action="store_true", help="Skip installation of the local plugin and marketplace entry.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = repo_root()
    home = Path(args.home).expanduser().resolve()

    skill_source = root / "skills" / PLUGIN_NAME
    plugin_source = root / "plugins" / PLUGIN_NAME
    if not skill_source.exists():
        raise SystemExit(f"Skill source not found: {skill_source}")
    if not plugin_source.exists():
        raise SystemExit(f"Plugin source not found: {plugin_source}")

    results: list[str] = []
    if not args.no_agents_skill:
        results.append(install_skill(skill_source, home / ".agents" / "skills" / PLUGIN_NAME, args.copy))
    if not args.no_codex_skill:
        results.append(install_skill(skill_source, home / ".codex" / "skills" / PLUGIN_NAME, args.copy))
    if not args.no_plugin:
        results.extend(
            install_plugin(
                plugin_source,
                home / "plugins",
                home / ".agents" / "plugins" / "marketplace.json",
                args.copy,
            )
        )

    print("Auto-Codex installation completed.")
    for line in results:
        print(f"- {line}")
    print()
    print("Next steps:")
    print("- Restart Codex CLI if it is already running.")
    print("- Restart the Codex app/plugin host if you want plugin discovery to rescan.")
    print("- In Codex CLI, use `$auto-codex ...` to invoke the skill.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
