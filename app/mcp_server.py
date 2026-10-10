"""Claude integration: an MCP server that drives Power Hour Maker.

Claude talks to this over stdio; this talks to the app's local backend over HTTP (starting a
hidden backend if the app isn't open). With the app window open you can watch Claude work.

Register (install-claude.ps1 does this):
    claude mcp add power-hour -s user -- "<app>\\.venv\\Scripts\\python.exe" -m app.mcp_server
"""
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

ROOT = Path(__file__).resolve().parent.parent
SERVER_INFO = ROOT / "data" / "server.json"
PYW = ROOT / ".venv" / "Scripts" / "pythonw.exe"

INSTRUCTIONS = """Power Hour Maker builds power hour drinking-game videos: N songs, each playing for one
minute (configurable), with a drink signal at every change, an intro and an outro.

Typical flow for "make me a <theme> power hour":
1. create_project(name, minutes) - 60 minutes = 60 songs at 60 s each (100 = Centurion).
2. Pick songs that fit the theme (well-known, singable, mixed eras/tempos, no duplicates). Add a
   few extra in case some fail. add_songs(["Artist - Title", ...], search_suffix="official video").
3. Style it: apply_theme() if a preset fits (see options()), then update_settings() for custom
   titles/colours/fonts/sounds; check the look with preview_image().
4. wait_for_downloads(), then list_songs(): replace_song() anything that failed or is the wrong
   video; choose_section() for long videos (movies, compilations) flagged "needs section".
5. Optionally fine-tune each song's minute with song_details() + set_song().
6. render() and poll render_status(); or export_for_editor() for Premiere/Resolve.
Songs can be referred to by position number, id, or part of the title."""

mcp = MCPServer("power-hour-maker", title="Power Hour Maker", instructions=INSTRUCTIONS)


# --- backend connection ----------------------------------------------------------------------

def _ping(url):
    try:
        with urllib.request.urlopen(url + "api/ping", timeout=2) as r:
            return json.load(r).get("app") == "power-hour-maker"
    except (OSError, ValueError):
        return False


def _registered():
    try:
        info = json.loads(SERVER_INFO.read_text("utf-8"))
        url = f"http://127.0.0.1:{int(info['port'])}/"
        return url if _ping(url) else None
    except (OSError, ValueError, KeyError):
        return None


def _spawn():
    exe = PYW if PYW.exists() else PYW.with_name("python.exe")
    cmd = [str(exe), "-m", "app", "--serve", "--idle-exit", "900"]
    detached, new_group, breakaway = 0x00000008, 0x00000200, 0x01000000
    kw = dict(cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:   # outlive this MCP process, even if Claude runs it inside a job object
        subprocess.Popen(cmd, creationflags=detached | new_group | breakaway, **kw)
    except OSError:
        subprocess.Popen(cmd, creationflags=detached | new_group, **kw)


_base = [None]


def _backend():
    if _base[0] and _ping(_base[0]):
        return _base[0]
    url = _registered()
    if not url:
        _spawn()
        deadline = time.time() + 60
        while not url and time.time() < deadline:
            time.sleep(0.5)
            url = _registered()
        if not url:
            raise ToolError("Could not start the Power Hour Maker backend (see data/app.log)")
    _base[0] = url
    return url


def _call(method, path, data=None, timeout=120, raw=False):
    req = urllib.request.Request(_backend() + path.lstrip("/"), method=method,
                                 data=None if data is None else json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except ValueError:
            msg = None
        raise ToolError(msg or f"HTTP {e.code}") from None
    except OSError as e:
        _base[0] = None
        raise ToolError(f"Power Hour Maker backend not reachable ({e}); try again") from None
    return body if raw else json.loads(body or b"{}")


def _get(path, **params):
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    return _call("GET", path + ("?" + q if q else ""))


def _post(path, data=None, timeout=120):
    return _call("POST", path, data or {}, timeout=timeout)


def _songs():
    return _get("/api/songs")


def _find(song):
    """Song by position, id or title text."""
    rows = _songs()["songs"]
    s = str(song).strip()
    if s.lstrip("#").isdigit():
        n = int(s.lstrip("#"))
        if 1 <= n <= len(rows):
            return rows[n - 1]
    for r in rows:
        if r["id"] == s:
            return r
    low = s.lower()
    hits = [r for r in rows if low in f"{r['title']} {r['artist']} {r.get('video_title') or ''}".lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ToolError(f"No song matches {song!r}")
    raise ToolError(f"{song!r} matches several songs: " + ", ".join(f"#{h['position']} {h['title']}" for h in hits[:6]))


def _state():
    return _get("/api/state")


# --- projects ----------------------------------------------------------------------------------

@mcp.tool()
def status() -> dict:
    """Current project, how many songs are ready/downloading/failed, total runtime, render progress,
    and the list of projects."""
    st = _state()
    p = st["project"]
    sm = st.get("summary") or {}
    projects = _get("/api/projects")["projects"]
    r = st.get("render") or {}
    return {
        "project": p["name"], "target_minutes": p["settings"]["target_minutes"],
        "seconds_per_song": p["settings"]["clip_seconds"], "songs_needed": sm.get("target"),
        "songs": {k: sm.get(k) for k in ("total", "ready", "pending", "error", "short", "long")},
        "runtime_seconds": sm.get("duration"),
        "render": {k: r.get(k) for k in ("status", "kind", "progress", "stage", "eta", "output", "error") if k in r},
        "projects": [x["name"] for x in projects],
    }


@mcp.tool()
def create_project(name: str, minutes: int = 60, seconds_per_song: int = 60, theme: str | None = None) -> dict:
    """Create a new project and switch to it. minutes = total length (60 = classic power hour,
    100 = Centurion, 120 = double); seconds_per_song = how long each song plays. Optional theme
    preset id (see options())."""
    _post("/api/projects/new", {"name": name})
    _call("PATCH", "/api/project", {"settings": {"target_minutes": minutes, "clip_seconds": seconds_per_song}})
    if theme:
        _post("/api/themes/apply", {"id": theme})
        _call("PATCH", "/api/project", {"settings": {"target_minutes": minutes, "clip_seconds": seconds_per_song}})
    return status()


@mcp.tool()
def open_project(name: str) -> dict:
    """Switch to an existing project (by name or id)."""
    projects = _get("/api/projects")["projects"]
    hit = next((p for p in projects if p["id"] == name), None) or next(
        (p for p in projects if p["name"].lower() == name.lower()), None) or next(
        (p for p in projects if name.lower() in p["name"].lower()), None)
    if not hit:
        raise ToolError(f"No project called {name!r}. Projects: " + ", ".join(p["name"] for p in projects))
    _post("/api/projects/open", {"id": hit["id"]})
    return status()


# --- songs ---------------------------------------------------------------------------------------

@mcp.tool()
def search_videos(query: str, limit: int = 6) -> list:
    """Search YouTube (or resolve a link from any site). Returns title, channel, duration, views,
    url. Use the url with add_songs or replace_song."""
    res = _get("/api/search", q=query, limit=limit)["results"]
    return [{k: r.get(k) for k in ("title", "channel", "duration", "views", "url", "in_project")} for r in res]


@mcp.tool()
def add_songs(songs: list[str], search_suffix: str = "") -> dict:
    """Add songs to the end of the playlist. Each item is "Artist - Title" (the top YouTube hit is
    used) or a link (YouTube, Vimeo, TikTok, ... ; playlist links add every video).
    search_suffix is appended to every text search, e.g. "official video" or "lyrics".
    Lookups and downloads run in the background: follow up with wait_for_downloads()."""
    text = "\n".join(s.strip() for s in songs if s and s.strip())
    r = _post("/api/clips/bulk", {"text": text, "suffix": search_suffix})
    return {"added": r.get("added"), "note": "Downloading in the background - call wait_for_downloads()."}


@mcp.tool()
def add_local_videos(path: str, include_subfolders: bool = False) -> dict:
    """Add video files from this computer: a single file or every video in a folder (sorted by
    name). Movies and other long files get status "needs section" - see choose_section()."""
    return _post("/api/clips/local", {"paths": [path], "recursive": include_subfolders})


@mcp.tool()
def list_songs() -> dict:
    """Every song in order: position, id, on-screen title/artist, status (ready, short, needs
    section, failed, not found, downloading...), the chosen minute (start/end seconds in the
    video) and what picked it (heatmap = YouTube "most replayed", loudness, manual...)."""
    return _songs()


@mcp.tool()
def wait_for_downloads(timeout_seconds: int = 180) -> dict:
    """Wait until lookups and downloads have finished (or the timeout passes), then report which
    songs still need attention."""
    deadline = time.time() + max(5, timeout_seconds)
    busy_states = {"searching", "queued"}
    while True:
        data = _songs()
        rows = data["songs"]
        busy = [r for r in rows if r["status"] in busy_states or r["status"].lower().startswith(
            ("downloading", "fetching", "analyzing", "cutting"))]
        if not busy or time.time() > deadline:
            break
        time.sleep(3)
    attention = [{k: r.get(k) for k in ("position", "title", "status", "error", "url", "suggested_section_start")
                  if r.get(k) is not None}
                 for r in rows if r["status"] in ("failed", "not found", "needs section")]
    return {"finished": not busy, "still_busy": len(busy), "summary": data["summary"],
            "needs_attention": attention}


@mcp.tool()
def song_details(song: str) -> dict:
    """Analysis of one song's video, to choose its minute well: duration, current window, the
    automatic suggestions, the strongest "most replayed" moments (YouTube heatmap) and the loudest
    stretches."""
    row = _find(song)
    st = _state()
    clip = next(c for c in st["project"]["clips"] if c["id"] == row["id"])
    if not clip.get("media_key"):
        return {"song": row, "note": "Not downloaded yet"}
    m = _get(f"/api/media/{urllib.parse.quote(clip['media_key'])}")
    hm = sorted(m.get("heatmap") or [], key=lambda h: -h[2])[:6]
    loud = m.get("loudness") or []
    peaks = sorted(range(len(loud)), key=lambda i: -loud[i])[:6]
    return {
        "song": row, "video_seconds": m.get("duration"), "suggested_starts": m.get("suggestions"),
        "automatic": {"start": m["auto"][0], "picked_by": m["auto"][1]} if m.get("auto") else None,
        "most_replayed_moments": [{"at": round((a + b) / 2, 1), "strength": round(v, 2)} for a, b, v in hm],
        "loudest_seconds": sorted(peaks),
        "excerpt_of_original": m.get("section"),
    }


@mcp.tool()
def set_song(song: str, start_seconds: float | None = None, title: str | None = None,
             artist: str | None = None, use_automatic_start: bool = False) -> dict:
    """Change one song: start_seconds moves its minute (seconds into the video); title/artist
    override the on-screen text; use_automatic_start=True returns to the automatic pick."""
    row = _find(song)
    data = {"id": row["id"]}
    if use_automatic_start:
        data["start"] = None
    elif start_seconds is not None:
        data["start"] = float(start_seconds)
    if title is not None:
        data["display_title"] = title
    if artist is not None:
        data["display_artist"] = artist
    _post("/api/clips/update", data)
    return _find(row["id"])


@mcp.tool()
def choose_section(song: str, start_seconds: float | None = None, minutes: float | None = None) -> dict:
    """For a long video (movie, concert, compilation): which part to use, as an excerpt of
    `minutes` (default 10) starting at start_seconds of the original. Without start_seconds the
    suggestion is used (centred on the most replayed part when known). Only that part is
    downloaded/cut; afterwards the song's minute is picked inside the excerpt."""
    row = _find(song)
    st = _state()
    clip = next(c for c in st["project"]["clips"] if c["id"] == row["id"])
    info = _get(f"/api/longvideo/{urllib.parse.quote(clip['media_key'])}")
    if start_seconds is None:
        start_seconds = info.get("suggestion")
        if start_seconds is None:
            start_seconds = float(info.get("duration") or 0) * 0.25
    length = float(minutes) * 60 if minutes else info["section_seconds"]
    _post("/api/clips/section", {"id": row["id"], "start": start_seconds, "length": length})
    return {"song": row["title"], "section_start": start_seconds, "section_seconds": length,
            "original_seconds": info.get("duration"), "note": "Cutting/downloading - call wait_for_downloads()."}


@mcp.tool()
def replace_song(song: str, query_or_url: str) -> dict:
    """Swap the video used for a song: a link, or a search (the top result is used)."""
    row = _find(song)
    res = _get("/api/search", q=query_or_url, limit=1)["results"]
    if not res:
        raise ToolError(f"Nothing found for {query_or_url!r}")
    _post("/api/clips/replace", {"id": row["id"], "entry": res[0]})
    return {"replaced": row["title"], "with": res[0]["title"], "url": res[0]["url"]}


@mcp.tool()
def remove_songs(songs: list[str]) -> dict:
    """Remove songs (by position, id or title)."""
    ids = [_find(s)["id"] for s in songs]
    _post("/api/clips/remove", {"ids": ids})
    return {"removed": len(ids)}


@mcp.tool()
def move_song(song: str, position: int) -> dict:
    """Move a song to a new position (1 = first)."""
    row = _find(song)
    ids = [r["id"] for r in _songs()["songs"] if r["id"] != row["id"]]
    ids.insert(max(0, min(len(ids), position - 1)), row["id"])
    _post("/api/clips/reorder", {"ids": ids})
    return {"moved": row["title"], "position": position}


@mcp.tool()
def shuffle_songs() -> dict:
    """Shuffle the playlist order."""
    _post("/api/clips/shuffle")
    return {"ok": True}


# --- look & feel ---------------------------------------------------------------------------------

@mcp.tool()
def options() -> dict:
    """Valid values for settings: fonts, sound effects, transition effects, theme presets."""
    m = _get("/api/meta")
    return {"themes": m["themes"], "fonts": m["fonts"], "sounds": m["sounds"],
            "transition_effects": [e["id"] for e in m["effects"]],
            "resolutions": ["1280x720", "1920x1080", "2560x1440", "3840x2160", "1080x1920", "1080x1080"],
            "overlay_positions": "tl, tr, bl, br (corners), mc (centre, countdown only)",
            "export_formats": m["export_formats"]}


@mcp.tool()
def apply_theme(theme: str) -> dict:
    """Apply a preset look (colours, fonts, intro/outro text, sounds). Ids from options()."""
    _post("/api/themes/apply", {"id": theme})
    return get_settings()


@mcp.tool()
def get_settings() -> dict:
    """The project's current look & feel: settings, overlays, transition, intro, outro."""
    p = _state()["project"]
    return {k: p[k] for k in ("name", "settings", "overlays", "transition", "intro", "outro")}


@mcp.tool()
def update_settings(changes: dict) -> dict:
    """Deep-merge changes into the project. Structure (all keys optional):
    name: str
    settings: target_minutes, clip_seconds, short_mode ("full"|"loop"), resolution ("1920x1080"...),
              fps, fill ("blur"|"black"|"crop"), normalize_audio (bool), quality ("draft"|"good"|"best")
    overlays: font, accent ("#rrggbb"), text_color,
              counter {enabled, position, label, format ("{n}", "{n}/{total}", "{left}"), scale, badge_text},
              title {enabled, seconds, position, show_artist}, progress {enabled, height, color},
              countdown {enabled, seconds, position},
              trivia {enabled, audio_seconds (sound only behind a cover, default 20), video_seconds (then the
              video without title, default 20; the answer is revealed after both), text, subtext ({n},{total}),
              reveal ("lower_third"|"big"), bg {type ("gradient"|"color"|"image"|"media"|"blur"), c1, c2, style,
              animated, dim}}  - guess-the-song mode
    transition: effect (see options), duration (s), random (bool), sfx (sound id), sfx_volume (0-1.5),
              sfx_first (bool), stinger {enabled, text, subtext ({n},{left},{next},{total}), duration,
              c1, c2, text_color, sfx}
    intro / outro: enabled, duration, title, subtitle ({count},{minutes},{name}), font, color, sub_color,
              bg {type ("gradient"|"color"|"image"|"media"), c1, c2, style ("linear"|"radial"|"circular"|
              "spiral"|"square"), animated, dim}, audio {type ("none"|"sfx"|"media"), sfx, volume},
              countdown (seconds, 0 = off), countdown_beeps, transition {effect, duration}
    Example: {"intro": {"title": "SPOOKY HOUR", "bg": {"c1": "#120018", "c2": "#ff6a00"}}}"""
    _call("PATCH", "/api/project", changes)
    return get_settings()


@mcp.tool()
def set_background_video(target: str, query_or_url: str, start_seconds: float = 0) -> dict:
    """Use a video as the background of the "intro", "outro", "stinger" or the "trivia" cover (e.g.
    "spooky fog loop", "fireworks 4k"). Downloads it, then switches that card to the video."""
    if target not in ("intro", "outro", "stinger", "trivia"):
        raise ToolError('target must be "intro", "outro", "stinger" or "trivia"')
    r = _post("/api/media/add", {"text": query_or_url})
    if target == "stinger":
        patch = {"transition": {"stinger": {"media_key": r["key"], "start": start_seconds}}}
    elif target == "trivia":
        patch = {"overlays": {"trivia": {"bg": {"type": "media", "media_key": r["key"], "start": start_seconds}}}}
    else:
        patch = {target: {"bg": {"type": "media", "media_key": r["key"], "start": start_seconds}}}
    _call("PATCH", "/api/project", patch)
    return {"video": r["title"], "target": target}


@mcp.tool()
def preview_image(what: str = "song", song: str | None = None, seconds: float = 3.0) -> Image:
    """A real rendered frame to check the look: what = "song" (first ready song unless `song` is
    given; seconds = moment within its minute - the title shows early, the countdown at the end),
    "intro", "outro" or "stinger"."""
    params = {"kind": "clip" if what == "song" else what, "t": seconds, "w": 960}
    if what == "song" and song:
        params["id"] = _find(song)["id"]
    q = urllib.parse.urlencode(params)
    return Image(data=_call("GET", f"/api/frame?{q}", raw=True), format="jpeg")


# --- output --------------------------------------------------------------------------------------

@mcp.tool()
def render(filename: str | None = None) -> dict:
    """Render the full video (songs that aren't downloaded yet are skipped). Runs in the background;
    poll render_status()."""
    _post("/api/render", {"filename": filename or ""})
    return render_status()


@mcp.tool()
def quick_preview() -> dict:
    """Render the first 3 songs at 480p (a few seconds) to check transitions and sounds. Returns the
    file path."""
    r = _post("/api/preview", {"kind": "quick"}, timeout=600)
    return {"file": r.get("path"), "seconds": r.get("duration")}


@mcp.tool()
def export_for_editor(formats: list[str] | None = None, handle_seconds: float = 5) -> dict:
    """Export an editable timeline + media for Adobe Premiere Pro (.xml), DaVinci Resolve (.otio/.xml)
    or any editor (.edl): every song as its own clip with extra footage on each side, overlays as
    PNG layers, drink sounds on their own track. Runs in the background; poll render_status()."""
    r = _post("/api/export", {"formats": formats or [], "handles": handle_seconds})
    return {"folder": r.get("folder"), "note": "Exporting - poll render_status()."}


@mcp.tool()
def render_status(wait_seconds: int = 0) -> dict:
    """Progress of the current render/export. wait_seconds > 0 waits (up to that long) for it to
    finish. When done, `output` is the video file (or export folder)."""
    deadline = time.time() + max(0, wait_seconds)
    while True:
        r = _get("/api/render")
        if r.get("status") != "running" or time.time() >= deadline:
            break
        time.sleep(3)
    keys = ("status", "kind", "progress", "stage", "eta", "elapsed", "output", "error", "written")
    out = {k: r.get(k) for k in keys if r.get(k) is not None}
    if "progress" in out:
        out["progress"] = f"{out['progress'] * 100:.0f}%"
    return out


@mcp.tool()
def open_app() -> dict:
    """Open the Power Hour Maker window so the user can watch, trim or play the result."""
    _post("/api/window")
    return {"ok": True}


def main():
    mcp.run("stdio")


if __name__ == "__main__":
    main()
