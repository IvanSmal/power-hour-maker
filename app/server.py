"""Local HTTP API + the static UI. Runs inside the desktop window (pywebview) or any browser."""
import copy
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import yt_dlp
from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from . import analysis, cards, ff, paths, render, sfx, themes
from . import export as export_mod
from . import media as media_mod
from . import project as project_mod

log = logging.getLogger(__name__)

app = Flask(__name__, static_folder=None)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 ** 3

settings = project_mod.AppSettings()
media = media_mod.MediaStore(settings.get)
projects = project_mod.ProjectStore()
renders = render.RenderManager()
preview_lock = threading.Lock()
resolver = ThreadPoolExecutor(4, thread_name_prefix="resolve")
ytdlp_update = {"status": "idle", "log": ""}
last_activity = [time.time()]

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus", ".wma"}


# ---------------------------------------------------------------------------------------------
# startup
# ---------------------------------------------------------------------------------------------

def init():
    pid = settings.get().get("last_project")
    if pid:
        try:
            projects.open(pid)
        except (OSError, ValueError):
            pass
    if not projects.project:
        existing = projects.list()
        if existing:
            projects.open(existing[0]["id"])
        else:
            projects.create()
    _resume()
    threading.Thread(target=ff.available_encoders, daemon=True).start()
    threading.Thread(target=lambda: [sfx.builtin(k) for k in sfx.SOUNDS], daemon=True).start()
    threading.Thread(target=render.available_effects, daemon=True).start()


def _resume():
    """Restart lookups/downloads that didn't finish (app closed, project switched...)."""
    p = projects.project
    settings.patch({"last_project": p["id"]})
    for c in p["clips"]:
        if c.get("resolve") == "searching" and c.get("query"):
            resolver.submit(_resolve_clip, p["id"], c["id"], c["query"], settings.get().get("search_source"))
        elif c.get("media_key") and c.get("url"):
            media.ensure(c["media_key"], c["url"], c)
    for key in _background_keys(p):
        m = media.get(key)
        if m:
            media.ensure(key, m["url"])


def _background_keys(p):
    keys = [p["intro"]["bg"].get("media_key"), p["outro"]["bg"].get("media_key"),
            (p["transition"].get("stinger") or {}).get("media_key"),
            (p["transition"].get("shot") or {}).get("media_key"),
            ((p["overlays"].get("trivia") or {}).get("bg") or {}).get("media_key")]
    return [k for k in keys if k]


def ok(**kw):
    return jsonify(ok=True, **kw)


def fail(msg, code=400):
    return jsonify(error=str(msg)), code


@app.errorhandler(Exception)
def _on_error(e):
    if isinstance(e, HTTPException):
        return jsonify(error=e.description), e.code
    log.exception("API error")
    return jsonify(error=str(e) or e.__class__.__name__), 500


def body():
    return request.get_json(force=True, silent=True) or {}


@app.before_request
def _touch():
    last_activity[0] = time.time()


def busy():
    """True while something is working in the background (used to decide when an idle backend may exit)."""
    p = projects.project or {}
    return (renders.busy() or bool(media.running) or preview_lock.locked()
            or any(c.get("resolve") == "searching" for c in p.get("clips", [])))


@app.get("/api/ping")
def ping():
    return jsonify(ok=True, app="power-hour-maker", pid=os.getpid(), project=(projects.project or {}).get("name"))


# ---------------------------------------------------------------------------------------------
# static UI
# ---------------------------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(paths.WEB, "index.html")


@app.get("/ui/<path:name>")
def ui_file(name):
    return send_from_directory(paths.WEB, name)


# ---------------------------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------------------------

def _builder(**kw):
    return render.PlanBuilder(projects.project, media, settings.get(), projects.assets_dir(), **kw)


def _summary(p):
    st = p["settings"]
    clip_s = float(st["clip_seconds"]) or 60
    target = max(1, math.ceil(float(st["target_minutes"]) * 60 / clip_s))
    counts = {"ready": 0, "pending": 0, "error": 0, "short": 0, "long": 0, "bonus_ready": 0}
    for c in p["clips"]:
        if c.get("resolve") == "error":
            counts["error"] += 1
            continue
        m = media.get(c["media_key"]) if c.get("media_key") else None
        if not m or c.get("resolve") == "searching":
            counts["pending"] += 1
        elif m["status"] == "ready":
            counts["bonus_ready" if c.get("bonus") else "ready"] += 1
            if (m.get("duration") or 0) < clip_s:
                counts["short"] += 1
        elif m["status"] == "error":
            counts["error"] += 1
        elif m["status"] == "long":
            counts["long"] += 1
        else:
            counts["pending"] += 1
    out = {"target": target, "total": len(p["clips"]), **counts, "duration": 0, "times": {}, "windows": {},
           "bonus": sum(1 for c in p["clips"] if c.get("bonus")),
           "shots": sum(1 for c in p["clips"] if c.get("shot"))}
    try:
        plan = _builder().build()
        out["duration"] = plan["duration"]
        out["times"] = {c["id"]: c["start"] for c in plan["chapters"] if c.get("id")}
        out["windows"] = {s["_id"]: {"ss": s["video"]["ss"], "dur": s["dur"], "src": s["_src"], "title": s["_title"],
                                     "artist": s["_artist"]} for s in plan["segments"] if s["kind"] == "clip"}
        out["encoder"] = plan["encoder"]
    except Exception:  # noqa: BLE001 - summary must never break polling
        log.exception("summary plan failed")
    return out


_last_summary = {"rev": None, "data": None}


@app.get("/api/state")
def state():
    rev = f"{projects.project['id']}.{projects.rev}.{media.rev}"
    if request.args.get("since") == rev:
        return jsonify(unchanged=True, rev=rev, render=renders.status())
    with projects.lock:
        p = copy.deepcopy(projects.project)
    keys = {c["media_key"] for c in p["clips"] if c.get("media_key")} | set(_background_keys(p))
    m = {k: media.get(k) for k in keys if media.get(k)}
    return jsonify(rev=rev, project=p, media=m, render=renders.status(), summary=_cached_summary())


@app.get("/api/meta")
def meta():
    return jsonify(
        fonts=list(cards.font_map().keys()),
        sounds=sfx.catalog(),
        effects=[{"id": e, "label": label} for e, label in render.available_effects()],
        encoders=[{"id": e, "label": ff.ENCODER_LABELS.get(e, e)} for e in ff.available_encoders()],
        sources=[{"id": k, "label": v[1]} for k, v in media_mod.SEARCH_SOURCES.items()],
        settings=settings.get(),
        ytdlp=yt_dlp.version.__version__,
        data_dir=str(paths.DATA),
        output_dir=str(paths.OUTPUT),
        themes=themes.catalog(),
        mcp_command=f'claude mcp add power-hour -s user -- "{paths.VENV_PY}" "{paths.ROOT / "power_hour_mcp.py"}"',
        export_formats=[{"id": k, "label": v} for k, v in export_mod.FORMATS.items()],
    )


@app.post("/api/settings")
def settings_patch():
    return jsonify(settings=settings.patch(body()))


# ---------------------------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------------------------

@app.get("/api/projects")
def projects_list():
    return jsonify(projects=projects.list(), current=projects.project["id"])


@app.post("/api/projects/new")
def projects_new():
    projects.create(body().get("name"))
    _resume()
    return ok()


@app.post("/api/projects/open")
def projects_open():
    projects.open(body()["id"])
    _resume()
    return ok()


@app.post("/api/projects/duplicate")
def projects_duplicate():
    projects.duplicate()
    _resume()
    return ok()


@app.post("/api/projects/delete")
def projects_delete():
    pid = body()["id"]
    if renders.busy():
        return fail("Wait for the render to finish first")
    projects.delete(pid)
    if not projects.project:
        rest = projects.list()
        if rest:
            projects.open(rest[0]["id"])
        else:
            projects.create()
        _resume()
    return ok()


@app.patch("/api/project")
def project_patch():
    projects.patch(body())
    return ok()


# ---------------------------------------------------------------------------------------------
# finding + adding songs
# ---------------------------------------------------------------------------------------------

def _annotate(results):
    in_project = {c.get("media_key") for c in projects.project["clips"]}
    for r in results:
        r["in_project"] = r["key"] in in_project
        r["cached"] = media.is_ready(r["key"])
    return results


@app.get("/api/search")
def search():
    q = request.args.get("q", "").strip()
    source = request.args.get("source") or "youtube"
    if not q:
        return jsonify(results=[])
    if source != settings.get().get("search_source"):
        settings.patch({"search_source": source})
    if media_mod.is_url(q):
        results = media.resolve(q, source)
    else:
        results = media.search(q, source, int(request.args.get("limit") or 12))
    return jsonify(results=_annotate(results))


@app.post("/api/clips/add")
def clips_add():
    b = body()
    entries = [e for e in b.get("entries") or [] if e.get("key") and e.get("url")]
    new = projects.add_clips(entries, b.get("index"))
    for e in entries:
        media.ensure(e["key"], e["url"], e)
    return ok(added=[c["id"] for c in new])


def _with_clip(pid, cid, fn):
    with projects.lock:
        if projects.project and projects.project["id"] == pid:
            c = projects.clip(cid)
            if c:
                fn(c)
                projects.save()


def _resolve_clip(pid, cid, text, source):
    try:
        res = [r for r in media.resolve(text, source or "youtube") if not r.get("live")]
        if not res:
            raise RuntimeError("No results")
    except Exception as e:  # noqa: BLE001
        _with_clip(pid, cid, lambda c: c.update(resolve="error", error=str(e)[:300]))
        return

    def apply(c, e):
        c.update(media_key=e["key"], url=e["url"], title=e["title"], channel=e.get("channel") or "",
                 thumbnail=e.get("thumbnail"), duration=e.get("duration"), resolve=None, error=None)

    if len(res) == 1:
        _with_clip(pid, cid, lambda c: apply(c, res[0]))
    else:  # a playlist link: the placeholder becomes every video in it
        with projects.lock:
            if projects.project and projects.project["id"] == pid and projects.clip(cid):
                clips = projects.project["clips"]
                idx = next(i for i, c in enumerate(clips) if c["id"] == cid)
                del clips[idx]
                projects.add_clips(res, idx)
    for e in res:
        media.ensure(e["key"], e["url"], e)


@app.post("/api/clips/bulk")
def clips_bulk():
    b = body()
    source = b.get("source") or "youtube"
    suffix = (b.get("suffix") or "").strip()
    lines = [x.strip() for x in (b.get("text") or "").splitlines()]
    lines = [x.lstrip("0123456789.)- \t") if not media_mod.is_url(x) else x for x in lines if x and not x.startswith("#")]
    lines = [x for x in lines if x]
    entries = [{"query": x if media_mod.is_url(x) or not suffix else f"{x} {suffix}", "title": x,
                "resolve": "searching"} for x in lines]
    new = projects.add_clips(entries)
    pid = projects.project["id"]
    for c in new:
        resolver.submit(_resolve_clip, pid, c["id"], c["query"], source)
    return ok(added=len(new))


@app.post("/api/clips/local")
def clips_local():
    b = body()
    files = []
    for p in b.get("paths") or []:
        files += _scan(p, bool(b.get("recursive")))
    return ok(added=_add_local_files(files))


@app.post("/api/clips/upload")
def clips_upload():
    entries = []
    for f in request.files.getlist("files"):
        key = media.save_upload(f.filename, f.stream)
        m = media.get(key)
        entries.append({"key": key, "url": m["url"], "title": Path(f.filename).stem, "channel": "Local file"})
    projects.add_clips(entries)
    return ok(added=len(entries))


@app.post("/api/clips/update")
def clips_update():
    b = body()
    fields = {}
    if "start" in b:
        fields["start"] = None if b["start"] is None else max(0.0, round(float(b["start"]), 2))
    for k in ("display_title", "display_artist"):
        if k in b:
            fields[k] = str(b[k] or "")[:200]
    if not projects.update_clip(b["id"], fields):
        return fail("No such clip", 404)
    return ok()


@app.post("/api/clips/flags")
def clips_flags():
    """{ids, shot?, bonus?}: mark songs as shot songs / bonus songs (bonus songs move to the end)."""
    b = body()
    fields = {k: bool(b[k]) for k in ("shot", "bonus") if k in b}
    if fields:
        projects.set_flags(b.get("ids") or [], fields)
    return ok()


@app.post("/api/clips/remove")
def clips_remove():
    projects.remove_clips(body().get("ids") or [])
    return ok()


@app.post("/api/clips/reorder")
def clips_reorder():
    projects.reorder(body().get("ids") or [])
    return ok()


@app.post("/api/clips/shuffle")
def clips_shuffle():
    projects.shuffle()
    return ok()


@app.post("/api/clips/reset_starts")
def clips_reset_starts():
    with projects.lock:
        for c in projects.project["clips"]:
            c["start"] = None
        projects.save()
    return ok()


@app.post("/api/clips/retry")
def clips_retry():
    ids = body().get("ids") or []
    p = projects.project
    for c in p["clips"]:
        if ids and c["id"] not in ids:
            continue
        if c.get("resolve") == "error" and c.get("query"):
            projects.update_clip(c["id"], {"resolve": "searching", "error": None})
            resolver.submit(_resolve_clip, p["id"], c["id"], c["query"], settings.get().get("search_source"))
        elif c.get("media_key"):
            m = media.get(c["media_key"])
            if not m or m["status"] == "error":
                media.ensure(c["media_key"], c["url"], c)
    return ok()


@app.post("/api/clips/replace")
def clips_replace():
    b = body()
    e = b["entry"]
    c = projects.update_clip(b["id"], {"media_key": e["key"], "url": e["url"], "title": e["title"],
                                       "channel": e.get("channel") or "", "thumbnail": e.get("thumbnail"),
                                       "duration": e.get("duration"), "start": None, "resolve": None,
                                       "error": None, "query": None})
    if not c:
        return fail("No such clip", 404)
    media.ensure(e["key"], e["url"], e)
    return ok()


# ---------------------------------------------------------------------------------------------
# media
# ---------------------------------------------------------------------------------------------

@app.get("/api/media/<key>")
def media_info(key):
    m = media.get(key, heavy=True)
    if not m:
        return fail("Unknown media", 404)
    L = float(projects.project["settings"]["clip_seconds"])
    m["suggestions"] = analysis.suggestions(m, L)
    m["auto"] = analysis.auto_start(m, L)
    return jsonify(m)


@app.post("/api/media/add")
def media_add():
    """A background/stinger video: download it without adding it to the song list."""
    b = body()
    if b.get("path"):
        key = media.add_local(b["path"])
        return ok(key=key, title=Path(b["path"]).stem)
    res = media.resolve(b.get("text") or "", b.get("source") or "youtube")
    if not res:
        return fail("Nothing found")
    e = res[0]
    media.ensure(e["key"], e["url"], e)
    return ok(key=e["key"], title=e["title"])


@app.post("/api/media/upload")
def media_upload():
    f = request.files["file"]
    key = media.save_upload(f.filename, f.stream)
    return ok(key=key, title=Path(f.filename).stem)


@app.get("/media/<key>")
def media_file(key):
    m = media.get(key)
    p = media.file_path(m) if m else None
    if not p or not p.exists():
        abort(404)
    return send_file(p, conditional=True)


# ---------------------------------------------------------------------------------------------
# project assets (images, music, sound files)
# ---------------------------------------------------------------------------------------------

def _store_asset(name, write):
    ext = Path(name).suffix.lower()
    if ext not in IMAGE_EXTS | AUDIO_EXTS:
        raise ValueError(f"Unsupported file type {ext}")
    stem = secure_filename(Path(name).stem)[:40] or "file"
    fname = f"{stem}-{uuid.uuid4().hex[:6]}{ext}"
    write(projects.assets_dir() / fname)
    return fname


@app.post("/api/assets/upload")
def assets_upload():
    f = request.files["file"]
    return ok(name=_store_asset(f.filename, f.save))


@app.post("/api/assets/import")
def assets_import():
    src = Path(body()["path"])
    return ok(name=_store_asset(src.name, lambda dst: shutil.copyfile(src, dst)))


@app.get("/assets/<name>")
def asset_file(name):
    return send_from_directory(projects.assets_dir(), name, conditional=True)


@app.get("/api/sound/<sid>")
def sound_file(sid):
    p = sfx.resolve(sid, projects.assets_dir())
    if not p:
        abort(404)
    return send_file(p, conditional=True)


# ---------------------------------------------------------------------------------------------
# previews
# ---------------------------------------------------------------------------------------------

def _preview_size(w=854):
    W, H = render.parse_res(projects.project["settings"]["resolution"])
    h = round(w * H / W / 2) * 2
    return w, h


@app.get("/api/frame")
def frame():
    kind = request.args.get("kind", "clip")
    t = float(request.args.get("t") or 2)
    b = _builder(size=_preview_size(int(request.args.get("w") or 960)))
    if kind == "clip":
        cid = request.args.get("id")
        plan = b.build()
        seg = next((s for s in plan["segments"] if s["kind"] == "clip" and (s.get("_id") == cid or not cid)), None)
        if not seg:
            return fail("No downloaded songs yet", 404)
        if request.args.get("rel") == "end":
            t = seg["dur"] - t
    elif kind in ("intro", "outro"):
        seg = b.single(b.card_segment(kind))["segments"][0]
    elif kind == "stinger":
        total = max(10, len(b.ready_clips()))
        seg = b.single(b.stinger_segment(3, total))["segments"][0]
    elif kind == "shot":
        seg = b.single(b.shot_segment(12, max(12, len(b.ready_clips()))))["segments"][0]
    else:
        return fail("Unknown kind")
    img = render.preview_frame(seg, t)
    return Response(cards.jpeg(img), mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/preview")
def preview():
    kind = body().get("kind")
    if not preview_lock.acquire(timeout=0.5):
        return fail("A preview is already rendering")
    try:
        pid = projects.project["id"]
        size = _preview_size()
        if kind == "transition":
            b = _builder(size=size, quality="draft", clip_len=5)
            ready = b.ready_clips()
            if len(ready) < 2:
                return fail("Download at least two songs to preview a transition")
            plan = b.build(clips=ready[:2], intro=False, outro=False)
        elif kind == "quick":
            b = _builder(size=size, quality="draft", clip_len=12)
            ready = b.ready_clips()
            if not ready:
                return fail("No downloaded songs yet")
            plan = b.build(clips=ready[:3])
        elif kind in ("intro", "outro"):
            b = _builder(size=size, quality="draft")
            plan = b.single(b.card_segment(kind))
        elif kind == "stinger":
            b = _builder(size=size, quality="draft")
            plan = b.single(b.stinger_segment(3, max(10, len(b.ready_clips()))))
        elif kind == "shot":
            b = _builder(size=size, quality="draft")
            plan = b.single(b.shot_segment(12, max(12, len(b.ready_clips()))))
        else:
            return fail("Unknown preview")
        wd = paths.WORK / pid / "preview"
        for old in wd.glob(f"{kind}-*.mp4"):
            old.unlink(missing_ok=True)
        out = wd / f"{kind}-{int(time.time() * 1000)}.mp4"
        job = render.RenderJob(plan, wd, out, workers=4, prune=True)
        job.run()
        return ok(url=f"/preview/{out.name}", path=str(out), duration=plan["duration"])
    finally:
        preview_lock.release()


@app.get("/preview/<name>")
def preview_file(name):
    return send_from_directory(paths.WORK / projects.project["id"] / "preview", name, conditional=True)


# ---------------------------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------------------------

@app.post("/api/render")
def render_start():
    if renders.busy():
        return fail("Already rendering")
    p = projects.project
    plan = _builder().build()
    if not plan["clip_count"]:
        return fail("No downloaded songs to render yet")
    name = (body().get("filename") or "").strip() or f"{p['name']} {datetime.now():%Y-%m-%d %H-%M}"
    out = paths.OUTPUT / (project_mod.safe_filename(name) + ".mp4")
    out.with_suffix(".txt").write_text(render.tracklist(plan), "utf-8")
    job = render.RenderJob(plan, paths.WORK / p["id"] / "full", out, workers=settings.get().get("render_workers") or 4,
                           title=p["name"], prune=True, project_id=p["id"])
    renders.start(job)
    return ok(status=renders.status())


@app.get("/api/render")
def render_status():
    return jsonify(renders.status())


@app.post("/api/render/cancel")
def render_cancel():
    renders.cancel()
    return ok()


@app.get("/api/tracklist")
def tracklist():
    return Response(render.tracklist(_builder().build()), mimetype="text/plain; charset=utf-8")


@app.get("/api/outputs")
def outputs():
    files = sorted(paths.OUTPUT.glob("*.mp4"), key=lambda p: -p.stat().st_mtime)
    return jsonify(files=[{"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime}
                          for p in files if not p.name.endswith(".part.mp4")])


@app.get("/output/<path:name>")
def output_file(name):
    return send_from_directory(paths.OUTPUT, name, conditional=True)


@app.post("/api/open")
def open_path():
    b = body()
    target = b.get("target")
    if target == "output-file":
        p = paths.OUTPUT / Path(b["name"]).name
        subprocess.Popen(["explorer", "/select,", str(p)], creationflags=ff.NO_WINDOW)
    elif target == "play":
        os.startfile(paths.OUTPUT / Path(b["name"]).name)  # noqa: S606 - opens in the user's player
    elif target == "data":
        os.startfile(paths.DATA)  # noqa: S606
    else:
        os.startfile(paths.OUTPUT)  # noqa: S606
    return ok()


# ---------------------------------------------------------------------------------------------
# housekeeping
# ---------------------------------------------------------------------------------------------

def _dir_size(d):
    return sum(f.stat().st_size for f in Path(d).rglob("*") if f.is_file())


@app.get("/api/storage")
def storage():
    return jsonify(cache=_dir_size(paths.CACHE), work=_dir_size(paths.WORK), output=_dir_size(paths.OUTPUT))


@app.post("/api/storage/clear")
def storage_clear():
    what = body().get("what")
    if renders.busy():
        return fail("Wait for the render to finish first")
    if what == "work":
        for d in paths.WORK.iterdir():
            shutil.rmtree(d, ignore_errors=True)
    elif what == "unused":
        used = set()
        for p in paths.PROJECTS.glob("*/project.json"):
            try:
                data = json.loads(p.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            data = project_mod.deep_merge(copy.deepcopy(project_mod.DEFAULT_PROJECT), data)
            used |= {c.get("media_key") for c in data["clips"]} | set(_background_keys(data))
        used |= {(media.get(k) or {}).get("parent") for k in list(used) if k}   # originals of sections
        removed = 0
        with media.lock:
            for key in [k for k in media.items if k not in used and k not in media.running]:
                m = media.items.pop(key)
                f = media.file_path(m)
                # local originals stay; sections cut out of them are our own copies in the cache
                if f and (not m.get("local") or paths.CACHE in Path(f).parents):
                    Path(f).unlink(missing_ok=True)
                (paths.CACHE / f"{key}.json").unlink(missing_ok=True)
                removed += 1
        return ok(removed=removed)
    return ok()


@app.post("/api/ytdlp/update")
def ytdlp_update_start():
    if ytdlp_update["status"] == "running":
        return ok(**ytdlp_update)

    def run():
        ytdlp_update.update(status="running", log="")
        try:
            r = subprocess.run([paths.UV or "uv", "pip", "install", "--python", str(paths.VENV_PY), "-U",
                                "yt-dlp[default]"], capture_output=True, text=True, creationflags=ff.NO_WINDOW,
                               timeout=300)
            ytdlp_update.update(status="done" if r.returncode == 0 else "error",
                                log=(r.stdout + r.stderr).strip()[-2000:])
        except Exception as e:  # noqa: BLE001
            ytdlp_update.update(status="error", log=str(e))

    threading.Thread(target=run, daemon=True).start()
    return ok(status="running")


@app.get("/api/ytdlp/update")
def ytdlp_update_status():
    return jsonify(ytdlp_update)


# ---------------------------------------------------------------------------------------------
# song list (compact, used by the Claude integration)
# ---------------------------------------------------------------------------------------------

def _cached_summary():
    rev = f"{projects.project['id']}.{projects.rev}.{media.rev}"
    if _last_summary["rev"] != rev:
        with projects.lock:
            p = copy.deepcopy(projects.project)
        _last_summary.update(rev=rev, data=_summary(p))
    return _last_summary["data"]


def _clip_state(c, clip_s):
    if c.get("resolve") == "searching":
        return "searching", None
    if c.get("resolve") == "error":
        return "not found", c.get("error")
    m = media.get(c["media_key"]) if c.get("media_key") else None
    if not m:
        return "queued", None
    st = m["status"]
    if st == "ready":
        return ("short" if (m.get("duration") or 0) < clip_s else "ready"), None
    if st == "long":
        return "needs section", None
    if st == "error":
        return "failed", m.get("error")
    return m.get("stage") or st, None


def _section_seconds():
    return float(settings.get().get("section_minutes") or 10) * 60


@app.get("/api/songs")
def songs():
    p = projects.project
    sm = _cached_summary()
    clip_s = float(p["settings"]["clip_seconds"])
    out = []
    n_bonus = 0
    for i, c in enumerate(p["clips"]):
        m = media.get(c["media_key"], heavy=True) if c.get("media_key") else None
        w = sm["windows"].get(c["id"])
        state, err = _clip_state(c, clip_s)
        row = {"position": i + 1, "id": c["id"],
               "title": (w or {}).get("title") or (m or {}).get("title") or c.get("title") or c.get("query"),
               "artist": (w or {}).get("artist") or "", "video_title": (m or {}).get("title") or c.get("title"),
               "status": state, "url": c.get("url"), "video_seconds": (m or {}).get("duration")}
        if err:
            row["error"] = err
        if c.get("shot"):
            row["shot"] = True
        if c.get("bonus"):
            n_bonus += 1
            row["bonus"] = f"B{n_bonus}"
        if w:
            row.update(start=w["ss"], end=round(w["ss"] + w["dur"], 2), picked_by=w["src"],
                       plays_at=sm["times"].get(c["id"]))
        if m and m.get("section"):
            row["excerpt_of_original"] = m["section"]
        if m and m.get("status") == "long":
            row["suggested_section_start"] = analysis.suggest_section(m, _section_seconds())
        out.append(row)
    keys = ("target", "total", "ready", "pending", "error", "short", "long", "duration", "bonus", "shots")
    return jsonify(project=p["name"], songs=out, summary={k: sm.get(k) for k in keys})


# ---------------------------------------------------------------------------------------------
# local folders
# ---------------------------------------------------------------------------------------------

def _natural(p):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(p))]


def _scan(path, recursive):
    root = Path(path)
    if root.is_file():
        return [root] if root.suffix.lower() in media_mod.VIDEO_EXTS else []
    if not root.is_dir():
        raise ValueError(f"Not found: {path}")
    found = root.rglob("*") if recursive else root.iterdir()
    return sorted((f for f in found if f.is_file() and f.suffix.lower() in media_mod.VIDEO_EXTS), key=_natural)[:1000]


def _add_local_files(files):
    entries = []
    for f in files:
        key = media.add_local(f)
        entries.append({"key": key, "url": f.resolve().as_uri(), "title": f.stem, "channel": "Local file"})
    projects.add_clips(entries)
    return len(entries)


@app.post("/api/clips/folder")
def clips_folder():
    b = body()
    files = _scan(b.get("path") or "", bool(b.get("recursive")))
    if not files:
        return fail("No video files in that folder")
    return ok(added=_add_local_files(files))


# ---------------------------------------------------------------------------------------------
# long videos: pick a section
# ---------------------------------------------------------------------------------------------

@app.get("/api/longvideo/<key>")
def longvideo(key):
    m = media.get(key, heavy=True)
    if not m:
        return fail("Unknown video", 404)
    parent = media.get(m.get("parent") or key, heavy=True)
    length = _section_seconds()
    return jsonify(key=parent["key"], title=parent.get("title"), duration=parent.get("duration"),
                   heatmap=parent.get("heatmap") or [], suggestion=analysis.suggest_section(parent, length),
                   section_seconds=length, current=m.get("section"), local=bool(parent.get("local")),
                   storyboard=bool(parent.get("storyboard")))


@app.get("/api/thumb/<key>")
def thumb(key):
    data = media.thumb(key, float(request.args.get("t") or 0), big=request.args.get("big") == "1")
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "max-age=86400"})


def _set_section(c, start, length):
    m = media.get(c["media_key"])
    parent = m.get("parent") or c["media_key"]
    key = media.create_section(parent, start, length)
    projects.update_clip(c["id"], {"media_key": key, "start": None})
    return key


@app.post("/api/clips/section")
def clips_section():
    b = body()
    c = projects.clip(b["id"])
    if not c or not c.get("media_key"):
        return fail("No such song", 404)
    return ok(key=_set_section(c, float(b.get("start") or 0), float(b.get("length") or _section_seconds())))


@app.post("/api/clips/auto_sections")
def clips_auto_sections():
    """Every song still waiting for a section gets the suggested one (most replayed, else 1/4 in)."""
    done = 0
    for c in list(projects.project["clips"]):
        m = media.get(c["media_key"], heavy=True) if c.get("media_key") else None
        if m and m.get("status") == "long":
            start = analysis.suggest_section(m, _section_seconds())
            if start is None:
                start = float(m.get("duration") or 0) * 0.25
            _set_section(c, start, _section_seconds())
            done += 1
    return ok(sections=done)


# ---------------------------------------------------------------------------------------------
# themes, export, windows
# ---------------------------------------------------------------------------------------------

@app.post("/api/themes/apply")
def themes_apply():
    t = themes.THEMES.get(body().get("id"))
    if not t:
        return fail("Unknown theme")
    projects.patch(copy.deepcopy(t["patch"]))
    return ok()


@app.post("/api/export")
def export_start():
    if renders.busy():
        return fail("Wait for the current render to finish")
    b = body()
    p = projects.project
    plan = _builder().build()
    if not plan["clip_count"]:
        return fail("No downloaded songs to export yet")
    out = export_mod.new_export_dir(p["name"], paths.EXPORTS)
    job = export_mod.ExportJob(plan, out, paths.WORK / p["id"] / "export", handles=float(b.get("handles", 5)),
                               formats=b.get("formats") or list(export_mod.FORMATS), title=p["name"],
                               project_id=p["id"], workers=settings.get().get("render_workers") or 4)
    renders.start(job)
    return ok(folder=str(out))


@app.get("/api/exports")
def exports_list():
    dirs = sorted((d for d in paths.EXPORTS.iterdir() if d.is_dir()), key=lambda d: -d.stat().st_mtime)
    return jsonify(exports=[{"name": d.name, "path": str(d), "mtime": d.stat().st_mtime} for d in dirs[:20]])


@app.post("/api/open_export")
def open_export():
    d = (paths.EXPORTS / Path(body()["name"]).name).resolve()
    if d.parent != paths.EXPORTS.resolve() or not d.is_dir():
        return fail("Unknown export", 404)
    os.startfile(d)  # noqa: S606
    return ok()


@app.post("/api/window")
def open_window():
    """Open an app window on this backend (used when Claude started the backend headless)."""
    exe = paths.VENV_PYW if paths.VENV_PYW.exists() else paths.VENV_PY
    subprocess.Popen([str(exe), "-m", "app"], cwd=str(paths.ROOT), creationflags=ff.NO_WINDOW)
    return ok()
