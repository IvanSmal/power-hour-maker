"""Projects (song list + every look/feel setting) and global app settings, stored as JSON."""
import copy
import json
import os
import random
import re
import shutil
import threading
import time
import uuid

from . import paths

DEFAULT_PROJECT = {
    "name": "My Power Hour",
    "settings": {
        "target_minutes": 60,
        "clip_seconds": 60,
        "short_mode": "full",          # full = play short videos as-is, loop = loop to fill the minute
        "resolution": "1920x1080",
        "fps": 30,
        "fill": "blur",                # how to fit non-16:9 videos: blur | black | crop
        "normalize_audio": True,
        "quality": "good",
    },
    "overlays": {
        "font": "Impact",
        "accent": "#ffc400",
        "text_color": "#ffffff",
        "counter": {"enabled": True, "position": "tr", "label": "DRINK", "format": "{n}", "scale": 1.0,
                    "badge_text": "#111111"},
        "title": {"enabled": True, "seconds": 7, "position": "bl", "show_artist": True},
        "progress": {"enabled": True, "height": 8, "color": "#ffc400"},
        "countdown": {"enabled": True, "seconds": 5, "position": "br"},
        # Guess-the-song: sound only behind a cover, then the video, then the title is revealed.
        "trivia": {
            "enabled": False,
            "audio_seconds": 20,        # sound only, the video is hidden behind the cover
            "video_seconds": 20,        # then the video without its title; the answer comes after
            "text": "GUESS THE SONG",
            "subtext": "#{n}",
            "reveal": "lower_third",    # lower_third | big
            "bg": {"type": "gradient", "c1": "#1a0b3d", "c2": "#3b1c8c", "style": "radial", "animated": True,
                   "speed": 0.015, "image": "", "media_key": None, "start": 0, "dim": 0.35},
        },
    },
    "transition": {
        "effect": "fade",
        "duration": 0.6,
        "random": False,
        "sfx": "airhorn",               # built-in sound id, "file:<asset>" or "none"
        "sfx_volume": 0.8,
        "sfx_first": True,              # also blast it when the first song starts
        "stinger": {
            "enabled": False,
            "text": "DRINK!",
            "subtext": "{n} down · {left} to go",
            "duration": 2.5,
            "c1": "#ff2e63",
            "c2": "#7b2ff7",
            "text_color": "#ffffff",
            "sfx": "airhorn3",
            "media_key": None,
            "start": 0,
        },
        # Card played before every song marked as a shot song (replaces the stinger there).
        "shot": {
            "text": "TAKE A SHOT!",
            "subtext": "no sipping · down it!",
            "duration": 4.0,
            "c1": "#7a2a00",                # whiskey brown, dark at the edges
            "c2": "#140400",
            "text_color": "#ffffff",
            "glass": True,              # draw a shot glass above the text
            "sfx": "gong",
            "media_key": None,          # optional video instead of the colour card
            "start": 0,
        },
    },
    "intro": {
        "enabled": True,
        "duration": 10,
        "title": "POWER HOUR",
        "subtitle": "{count} songs · {count} shots · no mercy",
        "font": "Impact",
        "color": "#ffffff",
        "sub_color": "#ffc400",
        "bg": {"type": "gradient", "c1": "#2b0548", "c2": "#ff2e63", "style": "linear", "animated": True,
               "speed": 0.015, "image": "", "media_key": None, "start": 0, "dim": 0.35},
        "audio": {"type": "sfx", "sfx": "gong", "file": "", "start": 0, "volume": 1.0},
        "countdown": 5,
        "countdown_beeps": True,
        "transition": {"effect": "fadeblack", "duration": 1.0},
    },
    "outro": {
        "enabled": True,
        "duration": 10,
        "title": "YOU SURVIVED",
        "subtitle": "{count} songs · {minutes} minutes",
        "font": "Impact",
        "color": "#ffffff",
        "sub_color": "#ffc400",
        "bg": {"type": "gradient", "c1": "#ff2e63", "c2": "#2b0548", "style": "radial", "animated": True,
               "speed": 0.015, "image": "", "media_key": None, "start": 0, "dim": 0.35},
        "audio": {"type": "sfx", "sfx": "airhorn3", "file": "", "start": 0, "volume": 1.0},
        "countdown": 0,
        "countdown_beeps": False,
        "transition": {"effect": "fadeblack", "duration": 1.5},
    },
    # Songs marked bonus play after the outro ("hidden tracks"), counted as BONUS 1, BONUS 2, ...
    "bonus": {"gap": 3.0, "label": "BONUS"},
    "clips": [],
}

PATCHABLE = ("name", "settings", "overlays", "transition", "intro", "outro", "bonus")
CLIP_FIELDS = ("start", "display_title", "display_artist")

APP_DEFAULTS = {
    "max_height": 1080,
    "concurrent_downloads": 3,
    "download_pause": 3,         # seconds (randomised up to 2x) before each download; avoids YouTube bot checks
    "cookies_browser": "",
    "cookies_file": "",
    "encoder": "auto",
    "render_workers": 4,
    "last_project": None,
    "search_source": "youtube",
    "long_minutes": 15,          # videos longer than this are used through a section, not downloaded whole
    "section_minutes": 10,       # default section length for long videos
}


def deep_merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def _atomic_write(path, data):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), "utf-8")
    os.replace(tmp, path)


class AppSettings:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = dict(APP_DEFAULTS)
        try:
            self.data.update(json.loads(paths.SETTINGS.read_text("utf-8")))
        except (OSError, ValueError):
            pass

    def get(self):
        with self.lock:
            return dict(self.data)

    def patch(self, changes):
        with self.lock:
            for k, v in changes.items():
                if k in APP_DEFAULTS:
                    self.data[k] = v
            _atomic_write(paths.SETTINGS, self.data)
            return dict(self.data)


class ProjectStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.project = None
        self.rev = 0                    # bumps on every change so the UI can poll cheaply

    # --- files ---------------------------------------------------------------------------
    def dir(self, pid=None):
        return paths.PROJECTS / (pid or self.project["id"])

    def assets_dir(self, pid=None):
        d = self.dir(pid) / "assets"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save(self):
        with self.lock:
            self.rev += 1
            self.project["updated"] = time.time()
            d = self.dir()
            d.mkdir(parents=True, exist_ok=True)
            _atomic_write(d / "project.json", self.project)

    def list(self):
        out = []
        for p in paths.PROJECTS.glob("*/project.json"):
            try:
                data = json.loads(p.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"id": data.get("id"), "name": data.get("name"), "updated": data.get("updated", 0),
                        "clips": len(data.get("clips", []))})
        return sorted(out, key=lambda x: -x["updated"])

    # --- lifecycle ------------------------------------------------------------------------
    def create(self, name=None):
        with self.lock:
            p = copy.deepcopy(DEFAULT_PROJECT)
            p["id"] = "p_" + uuid.uuid4().hex[:10]
            p["name"] = (name or "").strip() or DEFAULT_PROJECT["name"]
            p["created"] = time.time()
            self.project = p
            self.save()
            return p

    def open(self, pid):
        with self.lock:
            raw = json.loads((self.dir(pid) / "project.json").read_text("utf-8"))
            # Fill in settings added since the project was saved.
            self.project = deep_merge(copy.deepcopy(DEFAULT_PROJECT), raw)
            self.rev += 1
            return self.project

    def duplicate(self):
        with self.lock:
            src = self.dir()
            p = copy.deepcopy(self.project)
            p["id"] = "p_" + uuid.uuid4().hex[:10]
            p["name"] = p["name"] + " (copy)"
            p["created"] = time.time()
            if (src / "assets").exists():
                shutil.copytree(src / "assets", self.dir(p["id"]) / "assets")
            self.project = p
            self.save()
            return p

    def delete(self, pid):
        with self.lock:
            shutil.rmtree(self.dir(pid), ignore_errors=True)
            shutil.rmtree(paths.WORK / pid, ignore_errors=True)
            if self.project and self.project["id"] == pid:
                self.project = None

    def patch(self, changes):
        with self.lock:
            for k, v in changes.items():
                if k not in PATCHABLE:
                    continue
                if isinstance(v, dict) and isinstance(self.project.get(k), dict):
                    deep_merge(self.project[k], v)
                else:
                    self.project[k] = v
            self.save()
            return self.project

    # --- clips -------------------------------------------------------------------------------
    def clip(self, cid):
        return next((c for c in self.project["clips"] if c["id"] == cid), None)

    def add_clips(self, entries, index=None):
        """entries: dicts with url/key/title/... or {'query': text} placeholders still to resolve."""
        with self.lock:
            new = []
            for e in entries:
                new.append({
                    "id": "c_" + uuid.uuid4().hex[:10],
                    "media_key": e.get("key"),
                    "url": e.get("url"),
                    "query": e.get("query"),
                    "title": e.get("title") or e.get("query") or e.get("url") or "",
                    "channel": e.get("channel") or "",
                    "thumbnail": e.get("thumbnail"),
                    "duration": e.get("duration"),
                    "start": None,
                    "display_title": "",
                    "display_artist": "",
                    "resolve": e.get("resolve"),     # 'searching' | 'error' while a query is looked up
                    "error": None,
                    "shot": False,                   # a "take a shot" card plays before it
                    "bonus": False,                  # plays after the outro
                })
            clips = self.project["clips"]
            if index is None or index >= len(clips):
                clips.extend(new)
            else:
                clips[index:index] = new
            self._partition()
            self.save()
            return new

    def update_clip(self, cid, fields, save=True):
        with self.lock:
            c = self.clip(cid)
            if not c:
                return None
            c.update(fields)
            if save:
                self.save()
            return c

    def remove_clips(self, ids):
        with self.lock:
            ids = set(ids)
            self.project["clips"] = [c for c in self.project["clips"] if c["id"] not in ids]
            self.save()

    def reorder(self, ids):
        with self.lock:
            by_id = {c["id"]: c for c in self.project["clips"]}
            ordered = [by_id.pop(i) for i in ids if i in by_id]
            self.project["clips"] = ordered + list(by_id.values())
            self._partition()
            self.save()

    def shuffle(self):
        """Shuffle the main songs; bonus songs keep their order at the end."""
        with self.lock:
            main = [c for c in self.project["clips"] if not c.get("bonus")]
            random.shuffle(main)
            self.project["clips"] = main + [c for c in self.project["clips"] if c.get("bonus")]
            self.save()

    def set_flags(self, ids, fields):
        """Set shot / bonus on several clips at once. A song that changes group goes to the end of
        its new group (so bonus songs are numbered in the order they were marked)."""
        with self.lock:
            ids = set(ids)
            clips = self.project["clips"]
            moved = {c["id"] for c in clips
                     if c["id"] in ids and "bonus" in fields and bool(c.get("bonus")) != fields["bonus"]}
            for c in clips:
                if c["id"] in ids:
                    c.update(fields)
            self.project["clips"] = [c for c in clips if c["id"] not in moved] + \
                                    [c for c in clips if c["id"] in moved]
            self._partition()
            self.save()

    def _partition(self):
        """Bonus songs always sit after the main songs (stable, so both keep their own order)."""
        clips = self.project["clips"]
        self.project["clips"] = [c for c in clips if not c.get("bonus")] + [c for c in clips if c.get("bonus")]


_SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name):
    name = _SAFE.sub("_", name).strip(" .")
    return name[:120] or "power_hour"
