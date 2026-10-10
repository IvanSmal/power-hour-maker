"""Register the Power Hour Maker MCP server (and skill) with Claude Code and/or Claude Desktop.

    .venv\\Scripts\\python.exe -m app.install_claude [--code] [--desktop] [--uninstall]

With no target flags both are done. Config files are backed up (*.bak-powerhour) before changes.
"""
import argparse
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "power-hour"
SERVER = {"command": str(ROOT / ".venv" / "Scripts" / "python.exe"), "args": [str(ROOT / "power_hour_mcp.py")]}
SKILL_SRC = ROOT / ".claude" / "skills" / "power-hour" / "SKILL.md"


def _edit_json(path, change):
    path = Path(path)
    data = {}
    if path.exists():
        data = json.loads(path.read_text("utf-8") or "{}")
        shutil.copyfile(path, path.with_name(path.name + ".bak-powerhour"))
    change(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
    os.replace(tmp, path)


def claude_code(uninstall=False):
    cfg = Path.home() / ".claude.json"

    def change(d):
        servers = d.setdefault("mcpServers", {})
        if uninstall:
            servers.pop(NAME, None)
        else:
            servers[NAME] = {"type": "stdio", **SERVER, "env": {}}

    _edit_json(cfg, change)
    skill_dir = Path.home() / ".claude" / "skills" / "power-hour"
    if uninstall:
        shutil.rmtree(skill_dir, ignore_errors=True)
    else:
        skill_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SKILL_SRC, skill_dir / "SKILL.md")
    print(f"Claude Code: {'removed from' if uninstall else 'added to'} {cfg} (+ skill in {skill_dir})")


def claude_desktop(uninstall=False):
    cfg = Path(os.environ.get("APPDATA", "")) / "Claude" / "claude_desktop_config.json"

    def change(d):
        servers = d.setdefault("mcpServers", {})
        if uninstall:
            servers.pop(NAME, None)
        else:
            servers[NAME] = dict(SERVER)

    _edit_json(cfg, change)
    print(f"Claude Desktop: {'removed from' if uninstall else 'added to'} {cfg} - restart Claude Desktop")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--code", action="store_true", help="Claude Code (user-wide)")
    ap.add_argument("--desktop", action="store_true", help="Claude Desktop chat")
    ap.add_argument("--uninstall", action="store_true")
    a = ap.parse_args()
    both = not (a.code or a.desktop)
    if a.code or both:
        claude_code(a.uninstall)
    if a.desktop or both:
        claude_desktop(a.uninstall)


if __name__ == "__main__":
    main()
