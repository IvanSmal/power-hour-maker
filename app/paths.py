"""Filesystem layout. Everything lives under the app folder so the install stays portable."""
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
WEB = ROOT / "web"
DATA = ROOT / "data"
CACHE = DATA / "cache"          # downloaded source videos + metadata, shared by all projects
PROJECTS = DATA / "projects"
OUTPUT = DATA / "output"
EXPORTS = DATA / "exports"        # editable timelines for Premiere / Resolve
WORK = DATA / "work"            # render scratch: segment cache, previews
SFX = DATA / "sfx"              # generated sound effects
LOG = DATA / "app.log"
SETTINGS = DATA / "settings.json"
SERVER_INFO = DATA / "server.json"  # port of the running backend, so windows and Claude share it
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
VENV_PYW = ROOT / ".venv" / "Scripts" / "pythonw.exe"


def _tool(name):
    p = BIN / f"{name}.exe"
    if p.exists():
        return str(p)
    return shutil.which(name)


if BIN.exists():
    # Some yt-dlp code paths (partial downloads) only look for ffmpeg/deno on PATH.
    os.environ["PATH"] = str(BIN) + os.pathsep + os.environ.get("PATH", "")

FFMPEG = _tool("ffmpeg")
FFPROBE = _tool("ffprobe")
DENO = _tool("deno")
UV = _tool("uv")

for _d in (DATA, CACHE, PROJECTS, OUTPUT, EXPORTS, WORK, SFX):
    _d.mkdir(parents=True, exist_ok=True)
