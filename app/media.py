"""Searching, downloading and analysing source videos. The cache is shared by every project."""
import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadCancelled

from . import analysis, ff, paths

log = logging.getLogger(__name__)

# yt-dlp also has Google/Bilibili/Niconico search keys, but they come back empty or untitled
# these days. Any other site works by pasting a link.
SEARCH_SOURCES = {
    "youtube": ("ytsearch", "YouTube"),
}
VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".flv", ".wmv", ".mpg", ".mpeg", ".ts", ".3gp", ".ogv")
HEAVY_FIELDS = ("heatmap", "loudness", "storyboard")
# Download errors that won't fix themselves on a retry.
_PERMANENT = ("private video", "video unavailable", "been removed", "confirm your age", "not available in your",
              "live streams", "no video track", "unsupported url", "members-only", "premieres in", "is not a valid",
              "copyright", "geo restricted", "playlist is empty", "file is missing", "original file is gone")
_URL_RE = re.compile(r"^(https?://|www\.)\S+$", re.I)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def is_url(text):
    return bool(_URL_RE.match(text.strip()))


_VIMEO = re.compile(r"^https?://(?:www\.)?vimeo\.com/(?:channels/[\w-]+/|groups/[\w-]+/videos/|album/\d+/video/|"
                    r"showcase/\d+/video/|video/)?(\d+)(?:/([0-9a-f]{6,}))?(?:[/?#]|$)", re.I)


def normalize_url(url):
    """vimeo.com pages need a logged-in session in yt-dlp; the embeddable player URL doesn't."""
    m = _VIMEO.match(url or "")
    if m:
        return f"https://player.vimeo.com/video/{m.group(1)}" + (f"?h={m.group(2)}" if m.group(2) else "")
    return url


def make_key(ie_key, vid):
    return re.sub(r"[^a-z0-9_-]", "_", f"{ie_key or 'web'}_{vid}".lower())[:90]


def _short_error(msg):
    msg = _ANSI.sub("", str(msg)).replace("ERROR: ", "").strip()
    msg = re.sub(r"^\[[\w:]+\] [\w-]+: ", "", msg)
    return msg[:300]


class _YtLog:
    """yt-dlp logger: keeps errors, never touches stdout/stderr (they don't exist under pythonw)."""

    def __init__(self):
        self.errors = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        log.debug("yt-dlp: %s", msg)

    def error(self, msg):
        self.errors.append(msg)
        log.warning("yt-dlp: %s", msg)


def entry_from_info(e):
    """Normalise a yt-dlp search/playlist entry or full info dict into what the UI shows."""
    ie = e.get("ie_key") or e.get("extractor_key") or ""
    vid = e.get("id")
    url = normalize_url(e.get("webpage_url") or e.get("url") or "")
    thumbs = e.get("thumbnails") or []
    thumb = e.get("thumbnail") or (thumbs[-1].get("url") if thumbs else None)
    if ie.lower() == "youtube" and vid:
        url = f"https://www.youtube.com/watch?v={vid}"
        thumb = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
    key = make_key(ie, vid) if vid else make_key("url", hashlib.sha1(url.encode()).hexdigest()[:16])
    return {
        "key": key, "url": url, "title": e.get("title") or url,
        "channel": e.get("channel") or e.get("uploader") or "",
        "duration": e.get("duration"), "views": e.get("view_count"), "thumbnail": thumb,
        "live": e.get("live_status") in ("is_live", "is_upcoming"),
    }


class MediaStore:
    def __init__(self, get_settings):
        self.get_settings = get_settings
        self.items = {}
        self.rev = 0
        self.running = set()
        self.cancelled = set()
        self.lock = threading.RLock()
        self.gate = threading.Condition()
        self.active = 0
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="media")
        self._fetch_locks = {}
        self._load()

    # --- persistence ---------------------------------------------------------------
    def _load(self):
        for p in paths.CACHE.glob("*.json"):
            try:
                m = json.loads(p.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if m.get("status") not in ("ready", "long"):
                m.update(status="error", stage="Interrupted", error="Interrupted - press retry")
            self.items[m["key"]] = m

    def _save(self, key):
        with self.lock:
            data = json.dumps(self.items[key], ensure_ascii=False)
        p = paths.CACHE / f"{key}.json"
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(data, "utf-8")
        os.replace(tmp, p)

    def _set(self, key, **fields):
        with self.lock:
            self.items[key].update(fields)
            self.rev += 1

    # --- queries ---------------------------------------------------------------------
    def get(self, key, heavy=False):
        with self.lock:
            m = self.items.get(key)
            if m is None:
                return None
            if heavy:
                return dict(m)
            return {k: v for k, v in m.items() if k not in HEAVY_FIELDS} | {"has_heatmap": bool(m.get("heatmap"))}

    def file_path(self, m):
        f = m.get("file")
        if not f:
            return None
        p = Path(f)
        return p if p.is_absolute() else paths.CACHE / f

    def is_ready(self, key):
        m = self.get(key)
        if not m or m.get("status") != "ready":
            return False
        p = self.file_path(m)
        return bool(p and p.exists())

    # --- searching ---------------------------------------------------------------------
    def _ydl_opts(self, ylog):
        st = self.get_settings()
        o = {
            "quiet": True, "no_warnings": True, "noprogress": True, "logger": ylog,
            "socket_timeout": 20, "retries": 5, "fragment_retries": 5,
            "concurrent_fragment_downloads": 4, "noplaylist": True,
        }
        if paths.FFMPEG:
            o["ffmpeg_location"] = str(Path(paths.FFMPEG).parent)
        if paths.DENO:
            o["js_runtimes"] = {"deno": {"path": paths.DENO}}
        if st.get("cookies_browser"):
            o["cookiesfrombrowser"] = (st["cookies_browser"],)
        if st.get("cookies_file"):
            o["cookiefile"] = st["cookies_file"]
        return o

    def search(self, query, source="youtube", limit=12):
        prefix = SEARCH_SOURCES.get(source, SEARCH_SOURCES["youtube"])[0]
        ylog = _YtLog()
        try:
            with yt_dlp.YoutubeDL(self._ydl_opts(ylog) | {"extract_flat": "in_playlist"}) as y:
                r = y.extract_info(f"{prefix}{int(limit)}:{query}", download=False)
        except Exception as e:  # noqa: BLE001 - yt-dlp raises many types
            raise RuntimeError(_short_error(ylog.errors[-1] if ylog.errors else e)) from e
        return [entry_from_info(e) for e in (r or {}).get("entries") or [] if e]

    def resolve(self, text, source="youtube"):
        """A pasted URL (playlists expand to all their videos) or the top search hit for text."""
        text = text.strip()
        if not is_url(text):
            return self.search(text, source, 1)[:1]
        if text.lower().startswith("www."):
            text = "https://" + text
        text = normalize_url(text)
        ylog = _YtLog()
        try:
            with yt_dlp.YoutubeDL(self._ydl_opts(ylog) | {"extract_flat": "in_playlist"}) as y:
                r = y.extract_info(text, download=False)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(_short_error(ylog.errors[-1] if ylog.errors else e)) from e
        if r.get("_type") in ("playlist", "multi_video"):
            return [entry_from_info(e) for e in r.get("entries") or [] if e]
        return [entry_from_info(r)]

    # --- downloading ---------------------------------------------------------------------
    def ensure(self, key, url, hint=None):
        """Make sure media `key` is downloaded + analysed, queueing work if needed."""
        with self.lock:
            m = self.items.get(key)
            if m is None:
                m = {"key": key, "url": url, "status": "queued", "progress": 0, "stage": "Queued",
                     "added": time.time()}
                m.update({k: v for k, v in (hint or {}).items() if v is not None and k != "key"})
                self.items[key] = m
                self.rev += 1
            elif m.get("status") == "ready" and self.is_ready(key):
                return
            elif m.get("status") == "long":
                return          # waits for a section to be picked
            if key in self.running:
                return
            m.update(status="queued", stage="Queued", error=None, progress=0)
            self.running.add(key)
            self.cancelled.discard(key)
        self.pool.submit(self._job, key)

    def add_local(self, path):
        p = Path(path).resolve()
        st = p.stat()
        key = "local_" + hashlib.sha1(f"{p}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:14]
        hint = {"title": p.stem, "channel": "Local file", "file": str(p), "local": True}
        self.ensure(key, p.as_uri(), hint)
        return key

    def cancel(self, key):
        with self.lock:
            if key in self.running:
                self.cancelled.add(key)

    def _job(self, key):
        with self.gate:
            while self.active >= max(1, int(self.get_settings().get("concurrent_downloads") or 3)):
                self.gate.wait(1.0)
            self.active += 1
        try:
            m = self.get(key)
            for attempt in range(3):
                try:
                    if m.get("section"):
                        self._section(key)
                    elif m.get("local"):
                        self._analyze(key)
                    else:
                        self._download(key)
                    break
                except DownloadCancelled:
                    raise
                except Exception as e:  # noqa: BLE001 - YouTube 403s etc. are often transient
                    msg = str(e).lower()
                    if (m.get("local") and not m.get("section")) or attempt == 2 or any(w in msg for w in _PERMANENT):
                        raise
                    log.warning("media %s attempt %d failed: %s - retrying", key, attempt + 1, e)
                    self._set(key, status="queued", stage=f"Retrying ({attempt + 2}/3)...", progress=0)
                    time.sleep(3 + 5 * attempt)
        except DownloadCancelled:
            self._set(key, status="error", stage="Cancelled", error="Cancelled")
        except Exception as e:  # noqa: BLE001
            log.exception("media %s failed", key)
            self._set(key, status="error", stage="Failed", error=_short_error(e))
            self._save(key)
        finally:
            with self.lock:
                self.running.discard(key)
                self.cancelled.discard(key)
            with self.gate:
                self.active -= 1
                self.gate.notify_all()

    def _find_file(self, key):
        found = [p for p in paths.CACHE.glob(f"{glob_escape(key)}.*")
                 if p.suffix.lower() in VIDEO_EXTS and p.is_file()]
        return max(found, key=lambda p: p.stat().st_size) if found else None

    def _formats(self):
        h = int(self.get_settings().get("max_height") or 1080)
        # Sharpest picture first; at equal resolution prefer H.264, and avoid AV1 when there's a
        # choice so the in-app preview player can always play the file.
        fmt = f"bv*[height<={h}][vcodec!^=av01]+ba/bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
        return fmt, [f"res:{h}", "vcodec:h264", "acodec:m4a"]

    def _download(self, key):
        m = self.get(key)
        ylog = _YtLog()
        fmt, sort = self._formats()
        parts = [1]
        done_parts = []

        def hook(d):
            if key in self.cancelled:
                raise DownloadCancelled("cancelled")
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                frac = min(1.0, (d.get("downloaded_bytes") or 0) / total) if total else 0.0
                p = min(1.0, (len(done_parts) + frac) / parts[0])
                speed = d.get("speed")
                stage = f"Downloading {p * 100:.0f}%" + (f" - {speed / 1e6:.1f} MB/s" if speed else "")
                self._set(key, progress=round(p, 3), stage=stage)
            elif d["status"] == "finished":
                done_parts.append(d.get("filename"))

        opts = self._ydl_opts(ylog) | {
            "format": fmt, "format_sort": sort, "merge_output_format": "mp4",
            "outtmpl": str(paths.CACHE / f"{key}.%(ext)s"), "progress_hooks": [hook],
        }
        self._set(key, status="info", stage="Fetching info", progress=0)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(normalize_url(m["url"]), download=False)
                if info.get("_type") in ("playlist", "multi_video"):
                    entries = [e for e in info.get("entries") or [] if e]
                    if not entries:
                        raise RuntimeError("Playlist is empty")
                    info = ydl.extract_info(entries[0].get("webpage_url") or entries[0]["url"], download=False)
                if info.get("live_status") in ("is_live", "is_upcoming"):
                    raise RuntimeError("Live streams can't be used")
                hm = [[round(x["start_time"], 2), round(x["end_time"], 2), round(x["value"], 4)]
                      for x in info.get("heatmap") or []]
                self._set(key, title=info.get("title") or m.get("title"),
                          channel=info.get("channel") or info.get("uploader") or m.get("channel") or "",
                          duration=info.get("duration") or m.get("duration"), views=info.get("view_count"),
                          thumbnail=m.get("thumbnail") or info.get("thumbnail"), heatmap=hm,
                          webpage=info.get("webpage_url"), status="downloading", stage="Downloading")
                if (info.get("duration") or 0) > self.long_limit():
                    # Movies, compilations, concerts: don't pull the whole thing, wait for a section.
                    self._set(key, status="long", stage="Long video - pick a section", progress=0,
                              storyboard=_storyboards(info))
                    self._save(key)
                    return
                self._save(key)
                parts[0] = len(info.get("requested_formats") or []) or 1
                ydl.process_ie_result(info, download=True)
        except DownloadCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(_short_error(ylog.errors[-1] if ylog.errors else e)) from e
        f = self._find_file(key)
        if not f:
            raise RuntimeError(_short_error(ylog.errors[-1]) if ylog.errors else "Download produced no file")
        self._set(key, file=f.name)
        self._analyze(key)

    def _analyze(self, key):
        m = self.get(key)
        path = self.file_path(m)
        if not path or not path.exists():
            raise RuntimeError("File is missing")
        self._set(key, status="analyzing", stage="Analyzing audio", progress=1)
        info = ff.probe(path)
        if not info["has_video"]:
            raise RuntimeError("No video track in this file")
        if not m.get("section") and info["duration"] > self.long_limit():
            self._set(key, duration=round(info["duration"], 3), width=info["width"], height=info["height"],
                      has_audio=info["has_audio"], status="long", stage="Long video - pick a section",
                      progress=0, error=None)
            self._save(key)
            return
        loud = analysis.loudness_profile(path) if info["has_audio"] else []
        self._set(key, duration=round(info["duration"] or m.get("duration") or 0, 3),
                  width=info["width"], height=info["height"], has_audio=info["has_audio"],
                  loudness=loud, status="ready", stage="Ready", error=None, progress=1)
        self._save(key)

    # --- long videos: work on a 5-15 minute section instead of the whole thing --------------
    def long_limit(self):
        return float(self.get_settings().get("long_minutes") or 15) * 60

    def create_section(self, parent_key, start, length):
        """Media for `length` seconds of a long video starting at `start`; returns its key."""
        p = self.get(parent_key, heavy=True)
        if not p:
            raise ValueError("Unknown video")
        dur = float(p.get("duration") or 0)
        length = max(60.0, min(float(length), dur or float(length)))
        start = max(0.0, min(float(start), max(0.0, dur - length)))
        end = min(dur, start + length) if dur else start + length
        key = f"{parent_key}~{int(start)}-{int(end)}"
        hint = {k: p.get(k) for k in ("title", "channel", "thumbnail", "views", "local", "webpage")}
        hint.update(parent=parent_key, section=[int(start), int(end)])
        self.ensure(key, p["url"], hint)
        return key

    def _section(self, key):
        m = self.get(key, heavy=True)
        parent_key = m["parent"]
        parent = self.get(parent_key, heavy=True) or {}
        s, e = m["section"]
        src = self.file_path(parent) if parent.get("file") else None
        if not (src and src.exists()):
            if parent.get("local"):
                raise RuntimeError("The original file is gone")
            # yt-dlp can only fetch part of a video through ffmpeg, which YouTube throttles to a
            # crawl - so fetch the whole original once with the fast downloader (kept for re-picks).
            src = self._fetch_original(parent_key, report_key=key)
        out = paths.CACHE / f"{key}.mp4"
        tmp = out.with_name(out.stem + ".part.mp4")
        self._set(key, status="downloading", stage="Cutting the section", progress=0)
        if parent.get("local"):
            # Re-encode: movie files are often HEVC / AC3 / DTS, which the preview player can't play.
            ff.run(["-ss", s, "-t", e - s, "-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
                    "-vf", "scale='trunc(iw*sar/2)*2':ih,setsar=1,scale=-2:'min(ih,1080)'",
                    *_excerpt_video_args(), "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-movflags", "+faststart",
                    tmp], duration=e - s,
                   on_progress=lambda f: self._set(key, progress=round(f, 3),
                                                   stage=f"Cutting the section {f * 100:.0f}%"))
        else:
            ff.run(["-ss", s, "-t", e - s, "-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                    "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", tmp], duration=e - s)
        os.replace(tmp, out)
        # Most-replayed data of the original, shifted to the section's own clock.
        hm = [[round(max(a, s) - s, 2), round(min(b, e) - s, 2), v] for a, b, v in parent.get("heatmap") or []
              if b > s and a < e]
        self._set(key, file=out.name, heatmap=hm)
        self._analyze(key)

    def _fetch_original(self, parent_key, report_key):
        """Download a long online video in full (once); progress is shown on `report_key`."""
        with self._fetch_locks.setdefault(parent_key, threading.Lock()):
            existing = self._find_file(parent_key)
            if existing:
                self._set(parent_key, file=existing.name)
                return existing
            p = self.get(parent_key)
            ylog = _YtLog()

            def hook(d):
                if report_key in self.cancelled:
                    raise DownloadCancelled("cancelled")
                if d["status"] == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    frac = min(1.0, (d.get("downloaded_bytes") or 0) / total) if total else 0.0
                    speed = d.get("speed")
                    self._set(report_key, progress=round(frac, 3),
                              stage=f"Downloading the original {frac * 100:.0f}%"
                              + (f" - {speed / 1e6:.1f} MB/s" if speed else ""))

            fmt, sort = self._formats()
            opts = self._ydl_opts(ylog) | {"format": fmt, "format_sort": sort, "merge_output_format": "mp4",
                                           "outtmpl": str(paths.CACHE / f"{parent_key}.%(ext)s"),
                                           "progress_hooks": [hook]}
            self._set(report_key, status="downloading", stage="Downloading the original", progress=0)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([normalize_url(p["url"])])
            except DownloadCancelled:
                raise
            except Exception as ex:  # noqa: BLE001
                raise RuntimeError(_short_error(ylog.errors[-1] if ylog.errors else ex)) from ex
            f = self._find_file(parent_key)
            if not f:
                raise RuntimeError("Download produced no file")
            self._set(parent_key, file=f.name)
            self._save(parent_key)
            return f

    # --- thumbnails for picking a section ---------------------------------------------------
    def thumb(self, key, t, big=False):
        """JPEG bytes of the frame near `t` seconds of a (long) video; cached on disk."""
        m = self.get(key, heavy=True)
        if not m:
            raise ValueError("Unknown video")
        t = max(0.0, min(float(t), float(m.get("duration") or 0) - 1))
        d = paths.CACHE / "thumbs" / key
        d.mkdir(parents=True, exist_ok=True)
        out = d / f"{'L' if big else 'S'}_{int(t)}.jpg"
        if out.exists():
            return out.read_bytes()
        data = None
        boards = m.get("storyboard") or {}
        sb = boards.get("large" if big else "small") or boards.get("large")
        if sb:
            data = _storyboard_tile(sb, t, d)
        if data is None:
            src = self.file_path(m) if m.get("file") else None
            if not src or not src.exists():
                raise RuntimeError("No preview pictures for this video")
            data = ff.run_capture(["-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                                   "-vf", f"scale=-2:{360 if big else 120}", "-q:v", "4", "-f", "image2pipe",
                                   "-c:v", "mjpeg", "-"], timeout=60)
        out.write_bytes(data)
        return data

    def save_upload(self, filename, stream):
        """Browser-mode file upload: store in the cache and analyse."""
        ext = Path(filename).suffix.lower()
        if ext not in VIDEO_EXTS:
            raise ValueError(f"Unsupported video type: {ext or filename}")
        h = hashlib.sha1()
        tmp = paths.CACHE / f"upload_{int(time.time() * 1000)}.tmp"
        with open(tmp, "wb") as f:
            while chunk := stream.read(1 << 20):
                h.update(chunk)
                f.write(chunk)
        key = "upload_" + h.hexdigest()[:14]
        dest = paths.CACHE / f"{key}{ext}"
        os.replace(tmp, dest)
        self.ensure(key, dest.as_uri(), {"title": Path(filename).stem, "channel": "Local file",
                                         "file": dest.name, "local": True})
        return key


def glob_escape(s):
    return re.sub(r"([*?\[])", r"[\1]", s)


def _excerpt_video_args():
    """High-quality H.264 for cut sections (they become the source for the final render)."""
    if ff.pick_encoder("auto") == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "19", "-b:v", "0",
                "-profile:v", "high", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]


def _storyboards(info):
    """YouTube's thumbnail grids ('storyboards'): a small and a large tile size."""
    boards = [f for f in info.get("formats") or [] if f.get("format_note") == "storyboard" and f.get("fragments")]
    boards.sort(key=lambda f: f.get("height") or 0)
    out = {}
    for name, want in (("small", 90), ("large", 180)):
        pick = next((f for f in boards if (f.get("height") or 0) >= want), boards[-1] if boards else None)
        if pick:
            out[name] = {"w": pick["width"], "h": pick["height"], "rows": pick["rows"], "cols": pick["columns"],
                         "fps": pick["fps"], "frags": [fr["url"] for fr in pick["fragments"]]}
    return out


def _storyboard_tile(sb, t, cache_dir):
    """Cut the tile for time t out of the right storyboard sheet (sheets are cached)."""
    import urllib.request
    from io import BytesIO

    from PIL import Image
    per_sheet = sb["rows"] * sb["cols"]
    idx = int(t * sb["fps"])
    sheet_no, within = divmod(idx, per_sheet)
    if sheet_no >= len(sb["frags"]):
        sheet_no, within = len(sb["frags"]) - 1, per_sheet - 1
    sheet_path = cache_dir / f"sheet_{sb['h']}_{sheet_no}.jpg"
    try:
        if not sheet_path.exists():
            req = urllib.request.Request(sb["frags"][sheet_no], headers={"User-Agent": "Mozilla/5.0"})
            sheet_path.write_bytes(urllib.request.urlopen(req, timeout=20).read())
        sheet = Image.open(sheet_path)
    except OSError:
        return None
    row, col = divmod(within, sb["cols"])
    if (col + 1) * sb["w"] > sheet.width or (row + 1) * sb["h"] > sheet.height:
        return None
    tile = sheet.crop((col * sb["w"], row * sb["h"], (col + 1) * sb["w"], (row + 1) * sb["h"])).convert("RGB")
    buf = BytesIO()
    tile.save(buf, "JPEG", quality=85)
    return buf.getvalue()
