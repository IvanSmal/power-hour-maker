"""Turns a project into the finished video.

The timeline is a list of *segments* (intro, each song, optional "DRINK!" stingers, outro) with a
*join* between each neighbour. Every segment is rendered once into a body file; when a join is a
crossfade-style transition, the segment also emits short head/tail pieces. Each transition is then
rendered from the left segment's tail + the right segment's head, and the final file is a plain
stream-copy concat of body, transition, body, transition, ... with chapter markers.

Segments are cached by a hash of their full recipe, so re-rendering after nudging one song only
re-encodes that song (and the two transitions touching it).
"""
import hashlib
import json
import logging
import math
import os
import random
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageFilter

from . import analysis, cards, ff, paths, sfx, titles

log = logging.getLogger(__name__)

EFFECTS = [
    ("cut", "Hard cut"), ("fade", "Crossfade"), ("fadeblack", "Dip to black"), ("fadewhite", "Flash white"),
    ("dissolve", "Dissolve"), ("pixelize", "Pixelize"), ("radial", "Radial wipe"), ("circleopen", "Circle open"),
    ("circleclose", "Circle close"), ("zoomin", "Zoom in"), ("hblur", "Blur"), ("distance", "Distance"),
    ("wipeleft", "Wipe left"), ("wiperight", "Wipe right"), ("wipeup", "Wipe up"), ("wipedown", "Wipe down"),
    ("slideleft", "Slide left"), ("slideright", "Slide right"), ("slideup", "Slide up"), ("slidedown", "Slide down"),
    ("smoothleft", "Smooth left"), ("smoothright", "Smooth right"), ("coverleft", "Cover left"),
    ("revealright", "Reveal right"), ("squeezeh", "Squeeze"), ("diagtl", "Diagonal"), ("hlslice", "Slices"),
    ("vertopen", "Barn doors"), ("horzclose", "Shutter"), ("hlwind", "Wind"), ("fadegrays", "Fade through gray"),
]
_NOT_RANDOM = {"cut", "fadewhite", "fadegrays", "fade", "dissolve"}
LIMITER = "alimiter=limit=0.97:attack=3:release=60:level=0:latency=1"
AFMT = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
PREVIEW_SIZE = (854, 480)
_effects = None


def available_effects():
    """EFFECTS filtered to what this ffmpeg build's xfade supports."""
    global _effects
    if _effects is None:
        out = subprocess.run([paths.FFMPEG, "-hide_banner", "-h", "filter=xfade"], capture_output=True,
                             text=True, creationflags=ff.NO_WINDOW).stdout
        names = set(re.findall(r"^\s+(\w+)\s+-?\d+\s+\.\.FV", out, re.M))
        _effects = [(e, label) for e, label in EFFECTS if e == "cut" or e in names]
    return _effects


def snap(t, fps):
    return round(float(t) * fps) / fps


def floor_snap(t, fps):
    return math.floor(float(t) * fps + 1e-6) / fps


def hexc(c):
    return "0x%02x%02x%02x" % cards.rgb(c, (0, 0, 0))


def file_sig(path):
    st = os.stat(path)
    return f"{st.st_size}-{int(st.st_mtime)}"


class _Keep(dict):
    def __missing__(self, k):
        return "{" + k + "}"


def fmt_text(template, **ctx):
    try:
        return str(template or "").format_map(_Keep(ctx))
    except (ValueError, IndexError):
        return str(template or "")


def parse_res(res):
    try:
        w, h = (int(x) for x in str(res).lower().split("x"))
        return w // 2 * 2, h // 2 * 2
    except ValueError:
        return 1920, 1080


_sfx_durations = {}


def _sound_duration(path):
    key = (str(path), file_sig(path))
    if key not in _sfx_durations:
        _sfx_durations[key] = ff.probe(path)["duration"]
    return _sfx_durations[key]


def spec_hash(obj):
    """Content hash of a recipe; keys starting with '_' are bookkeeping and don't affect the output."""
    data = {k: v for k, v in obj.items() if not k.startswith("_")}
    return hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:20]


# ============================================================================================
# Planning
# ============================================================================================

class PlanBuilder:
    def __init__(self, project, media, app_settings, assets_dir, *, size=None, quality=None, clip_len=None):
        self.p = project
        self.media = media
        self.assets = Path(assets_dir)
        st = project["settings"]
        self.st, self.ov, self.tr = st, project["overlays"], project["transition"]
        self.W, self.H = size or parse_res(st.get("resolution"))
        self.fps = int(st.get("fps") or 30)
        self.L = snap(clip_len or st.get("clip_seconds") or 60, self.fps)
        self.quality = quality or st.get("quality") or "good"
        self.encoder = ff.pick_encoder(app_settings.get("encoder"))
        self.enc = ff.video_args(self.encoder, self.quality, self.fps, self.H)
        self.style = {"font": self.ov.get("font"), "accent": self.ov.get("accent"),
                      "text_color": self.ov.get("text_color")}
        pg = self.ov.get("progress") or {}
        self.bar_h = max(2, round(float(pg.get("height") or 8) * self.H / 1080)) if pg.get("enabled") else 0
        self.ctx = None

    # --- helpers -----------------------------------------------------------------------------
    def context(self):
        """Values for {count} / {minutes} / {name} placeholders in card text."""
        if self.ctx is None:
            ready = [c for c in self.ready_clips() if not c.get("bonus")]
            secs = 0.0
            for c in ready:
                d = float((self.media.get(c["media_key"]) or {}).get("duration") or 0)
                secs += self.L if (d > self.L or self.st.get("short_mode") == "loop") else d
            self.ctx = {"count": len(ready), "minutes": round(secs / 60), "name": self.p.get("name", "")}
        return self.ctx

    def sound(self, value, volume=1.0):
        path = sfx.resolve(value, self.assets)
        if not path:
            return None
        return {"path": str(path), "sig": file_sig(path), "volume": round(float(volume), 3),
                "dur": round(_sound_duration(path), 3)}

    def asset(self, name):
        if not name:
            return None
        p = self.assets / name
        return p if p.exists() else None

    def ready_clips(self):
        return [c for c in self.p["clips"] if c.get("media_key") and self.media.is_ready(c["media_key"])]

    # --- segment recipes ------------------------------------------------------------------------
    def clip_segment(self, c, i, total, bonus=None):
        """Recipe for one song; `bonus` is the label (e.g. "BONUS") for songs played after the outro."""
        fps, L, ov = self.fps, self.L, self.ov
        m = self.media.get(c["media_key"], heavy=True)
        path = self.media.file_path(m)
        vdur = float(m.get("duration") or 0)
        if vdur <= L + 0.05:
            if self.st.get("short_mode") == "loop":
                dur, ss, loop, src = L, 0.0, True, "loop"
            else:
                dur, ss, loop, src = max(floor_snap(vdur - 0.1, fps), 1.0), 0.0, False, "full"
        else:
            start, src = c.get("start"), "manual"
            if start is None:
                start, src = analysis.auto_start(m, L)
            dur, ss, loop = L, analysis.clamp_start(start, L, vdur), False
        title, artist = titles.display(c, m)
        n = i + 1
        overlays = []
        cnt = ov.get("counter") or {}
        if cnt.get("enabled"):
            overlays.append({"type": "counter", "n": n, "total": total,
                             "label": bonus if bonus else cnt.get("label", ""),
                             "format": "{n}" if bonus else (cnt.get("format") or "{n}"),
                             "position": cnt.get("position") or "tr",
                             "scale": cnt.get("scale") or 1, "badge_text": cnt.get("badge_text"), **self.style})
        tt = ov.get("title") or {}
        trivia = self.trivia_phases(dur)
        if trivia:
            audio_end, reveal_at = trivia
            tv = ov.get("trivia") or {}
            sub_t, n_t = tv.get("subtext") or "", n
            if bonus:                                   # "#{n}" -> "BONUS 1"
                sub_t, n_t = sub_t.replace("#{n}", "{n}"), f"{bonus} {n}"
            text, sub = fmt_text(tv.get("text"), n=n_t, total=total), fmt_text(sub_t, n=n_t, total=total)
            if audio_end and (text or sub):
                overlays.append({"type": "card_text", "title": text, "subtitle": sub, "font": self.style["font"],
                                 "color": self.style["text_color"], "sub_color": self.style["accent"], "scale": 0.75,
                                 "sub_scale": 1.8, "start": 0, "end": audio_end, "fade_in": 0.3, "fade_out": 0.4})
            if title and reveal_at < dur - 1:
                if tv.get("reveal") == "big":
                    overlays.append({"type": "card_text", "title": title,
                                     "subtitle": artist if tt.get("show_artist", True) else "",
                                     "font": self.style["font"], "color": self.style["text_color"],
                                     "sub_color": self.style["accent"], "scale": 0.8, "start": reveal_at,
                                     "end": round(dur - 0.3, 3), "fade_in": 0.3, "fade_out": 0.3})
                else:
                    overlays.append({"type": "lower_third", "title": title,
                                     "artist": artist if tt.get("show_artist", True) else "",
                                     "position": tt.get("position") or "bl", "bottom_extra": self.bar_h,
                                     "start": reveal_at, "end": round(dur - 0.3, 3), "fade_in": 0.35,
                                     "fade_out": 0.3, **self.style})
        elif tt.get("enabled") and title:
            end = min(dur - 0.5, 0.6 + float(tt.get("seconds") or 7))
            if end > 1.5:
                overlays.append({"type": "lower_third", "title": title,
                                 "artist": artist if tt.get("show_artist", True) else "",
                                 "position": tt.get("position") or "bl", "bottom_extra": self.bar_h,
                                 "start": 0.6, "end": round(end, 3), "fade_in": 0.35, "fade_out": 0.35,
                                 **self.style})
        spec = {
            "kind": "clip", "n": n, "_id": c.get("id"), "_src": src, "_title": title, "_artist": artist,
            "_tag": f"B{n:02d}" if bonus else f"{n:03d}", "_bonus": bool(bonus),
            # bonus songs stay a secret in the chapter list
            "label": f"{bonus.title()} {n}" if bonus else f"{n}. {title}" + (f" - {artist}" if artist else ""),
            "dur": dur,
            "video": {"type": "file", "path": str(path), "sig": file_sig(path), "ss": round(ss, 3), "loop": loop,
                      "src_dur": vdur, "fill": self.st.get("fill") or "blur", "src_w": m.get("width"),
                      "src_h": m.get("height")},
            "audio": {"type": "src" if m.get("has_audio") else "none"},
            "normalize": bool(self.st.get("normalize_audio")),
            "overlays": overlays,
            "progress": {"color": (ov.get("progress") or {}).get("color") or self.style["accent"],
                         "height": self.bar_h} if self.bar_h else None,
            "sfx": [], "fade_in": 0, "fade_out": 0, "dim": 0,
        }
        if trivia and trivia[0]:
            spec["cover"] = self.trivia_cover(trivia[0])
        cd = ov.get("countdown") or {}
        if cd.get("enabled") and int(cd.get("seconds") or 0) > 0:
            spec["countdown"] = {"n": int(cd["seconds"]), "position": cd.get("position") or "br", "beep": None}
        return spec

    def trivia_phases(self, dur):
        """(end of the sound-only part, moment the title is revealed) for a clip of `dur` seconds,
        squeezed proportionally when the clip is too short for the configured times; None if off."""
        tv = self.ov.get("trivia") or {}
        if not tv.get("enabled"):
            return None
        a = max(0.0, float(tv.get("audio_seconds") or 0))
        v = max(0.0, float(tv.get("video_seconds") or 0))
        room = max(0.0, dur - 3)            # leave a few seconds with the answer on screen
        if a + v > room and a + v > 0:
            a, v = a * room / (a + v), v * room / (a + v)
        a = snap(a, self.fps) if a >= 1 else 0.0
        return a, snap(a + v, self.fps)

    def trivia_cover(self, until):
        """What hides the picture during the sound-only part of a trivia clip."""
        bg = (self.ov.get("trivia") or {}).get("bg") or {}
        if bg.get("type") == "blur":
            video = {"type": "blur"}
        else:
            video, _ = self._card_video(bg, until)
        dim = float(bg.get("dim") or 0) if video["type"] in ("file", "image") else 0
        return {"video": video, "until": until, "dim": dim, "fade": 0.4}

    def _card_video(self, bg, dur):
        t = bg.get("type")
        key = bg.get("media_key")
        if t == "media" and key and self.media.is_ready(key):
            m = self.media.get(key)
            path = self.media.file_path(m)
            vdur = float(m.get("duration") or 0)
            ss = max(0.0, min(float(bg.get("start") or 0), max(0.0, vdur - 1)))
            return ({"type": "file", "path": str(path), "sig": file_sig(path), "ss": round(ss, 3),
                     "loop": vdur - ss < dur + 0.1, "src_dur": vdur, "fill": "crop", "src_w": m.get("width"),
                     "src_h": m.get("height")}, bool(m.get("has_audio")))
        img = self.asset(bg.get("image")) if t == "image" else None
        if img:
            return {"type": "image", "path": str(img), "sig": file_sig(img)}, False
        if t == "gradient":
            return ({"type": "gradient", "c1": bg.get("c1"), "c2": bg.get("c2"), "style": bg.get("style") or "linear",
                     "speed": float(bg.get("speed") or 0.015) if bg.get("animated", True) else 0}, False)
        return {"type": "color", "c1": bg.get("c1") or "#000000"}, False

    def card_segment(self, which):
        card = self.p[which]
        fps = self.fps
        dur = snap(max(2.0, float(card.get("duration") or 8)), fps)
        bg = card.get("bg") or {}
        video, src_audio = self._card_video(bg, dur)
        title = fmt_text(card.get("title"), **self.context())
        sub = fmt_text(card.get("subtitle"), **self.context())
        overlays = []
        if title or sub:
            overlays.append({"type": "card_text", "title": title, "subtitle": sub, "font": card.get("font"),
                             "color": card.get("color"), "sub_color": card.get("sub_color"),
                             "start": 0, "end": dur, "fade_in": 0.5 if which == "intro" else 0.3, "fade_out": 0})
        a = card.get("audio") or {}
        vol = float(a.get("volume") or 1)
        audio = {"type": "none"}
        events = []
        if a.get("type") == "media" and video["type"] == "file" and src_audio:
            audio = {"type": "src", "volume": vol}
        elif a.get("type") == "file" and self.asset(a.get("file")):
            f = self.asset(a.get("file"))
            audio = {"type": "file", "path": str(f), "sig": file_sig(f), "ss": float(a.get("start") or 0),
                     "volume": vol, "loop": True, "src_dur": _sound_duration(f),
                     "fade_out": round(min(2.0, dur / 3), 3)}
        elif a.get("type") == "sfx":
            ev = self.sound(a.get("sfx"), vol)
            if ev:
                events.append(ev | {"at": 0.2 if which == "intro" else 0.0, "offset": 0})
        spec = {
            "kind": which, "label": which.title(), "dur": dur, "video": video, "audio": audio,
            "normalize": bool(self.st.get("normalize_audio")) and audio["type"] != "none",
            "overlays": overlays, "progress": None, "sfx": events,
            "fade_in": 0.8 if which == "intro" else 0, "fade_out": 1.5 if which == "outro" else 0,
            "dim": float(bg.get("dim") or 0) if video["type"] in ("file", "image") else 0,
        }
        n = min(int(card.get("countdown") or 0), int(dur) - 2)
        if n > 0:
            spec["countdown"] = {"n": n, "position": "mc", "hide_title": True,
                                 "beep": self.sound("tick", 0.9) if card.get("countdown_beeps") else None}
        return spec

    def _flash_card(self, cfg, kind, ctx, c1, c2, glass=False):
        """A short full-screen card (stinger, shot): a colour gradient or a clip with big text on top."""
        fps = self.fps
        dur = snap(max(0.5, float(cfg.get("duration") or 2.5)), fps)
        key = cfg.get("media_key")
        if key and self.media.is_ready(key):
            m = self.media.get(key)
            path = self.media.file_path(m)
            vdur = float(m.get("duration") or 0)
            ss = max(0.0, min(float(cfg.get("start") or 0), max(0.0, vdur - 0.5)))
            dur = max(0.5, min(dur, floor_snap(vdur - ss - 0.05, fps)))
            video = {"type": "file", "path": str(path), "sig": file_sig(path), "ss": round(ss, 3), "loop": False,
                     "fill": self.st.get("fill") or "blur", "src_w": m.get("width"), "src_h": m.get("height")}
            audio = {"type": "src" if m.get("has_audio") else "none"}
            glass = False                                # the clip is the graphic
        else:
            video = {"type": "gradient", "c1": cfg.get("c1") or c1, "c2": cfg.get("c2") or c2,
                     "style": "radial", "speed": 0.06}
            audio = {"type": "none"}
        title, sub = fmt_text(cfg.get("text"), **ctx), fmt_text(cfg.get("subtext"), **ctx)
        color = cfg.get("text_color") or "#ffffff"
        overlays = []
        if glass:
            overlays.append({"type": "shot_glass", "scale": 1.45, "y": 0.05, "start": 0, "end": dur, "fade_in": 0.2,
                             "fade_out": 0})
        if title or sub:
            overlays.append({"type": "card_text", "title": title, "subtitle": sub, "font": self.style["font"],
                             "color": color, "sub_color": color, "scale": 1.0 if glass else 1.15,
                             "y_offset": 0.2 if glass else 0, "start": 0, "end": dur, "fade_in": 0, "fade_out": 0})
        return {"kind": kind, "label": kind.title(), "dur": dur, "video": video, "audio": audio,
                "normalize": False, "overlays": overlays, "progress": None, "sfx": [],
                "fade_in": 0, "fade_out": 0, "dim": 0}

    def stinger_segment(self, done, total):
        ctx = {"n": done, "left": total - done, "total": total, "next": done + 1}
        return self._flash_card(self.tr.get("stinger") or {}, "stinger", ctx, "#ff2e63", "#7b2ff7")

    def shot_segment(self, n, total):
        """The "take a shot" card before song `n` (a number, or e.g. "BONUS 1")."""
        cfg = self.tr.get("shot") or {}
        return self._flash_card(cfg, "shot", {"n": n, "total": total}, "#7a2a00", "#140400",
                                glass=cfg.get("glass", True))

    def gap_segment(self, dur):
        """Black silence between the outro and the bonus songs, like a hidden track on a CD."""
        return {"kind": "gap", "label": "Gap", "dur": snap(dur, self.fps), "video": {"type": "color", "c1": "#000000"},
                "audio": {"type": "none"}, "normalize": False, "overlays": [], "progress": None, "sfx": [],
                "fade_in": 0, "fade_out": 0, "dim": 0}

    # --- joins -------------------------------------------------------------------------------
    def main_join(self, index, sound):
        eff = self.tr.get("effect") or "cut"
        d = float(self.tr.get("duration") or 0)
        if self.tr.get("random"):
            pool = [e for e, _ in available_effects() if e not in _NOT_RANDOM]
            eff = random.Random(f"{self.p.get('id')}:{index}").choice(pool)
        if eff == "cut" or d <= 0:
            return {"effect": "cut", "d": 0, "sfx": sound}
        return {"effect": eff, "d": snap(d, self.fps), "sfx": sound}

    def card_join(self, which, sound):
        t = self.p[which].get("transition") or {}
        eff, d = t.get("effect") or "fadeblack", float(t.get("duration") or 0)
        if eff == "cut" or d <= 0:
            return {"effect": "cut", "d": 0, "sfx": sound}
        return {"effect": eff, "d": snap(d, self.fps), "sfx": sound}

    # --- assembling -------------------------------------------------------------------------------
    def build(self, clips=None, intro=True, outro=True, stinger=True):
        clips = self.ready_clips() if clips is None else clips
        main = [c for c in clips if not c.get("bonus")]
        extra = [c for c in clips if c.get("bonus")]
        total = len(main)
        bcfg = self.p.get("bonus") or {}
        blabel = (bcfg.get("label") or "").strip() or "BONUS"
        clip_specs = [self.clip_segment(c, i, total) for i, c in enumerate(main)]
        bonus_specs = [self.clip_segment(c, i, len(extra), bonus=blabel) for i, c in enumerate(extra)]
        self.ctx = {"count": total, "minutes": round(sum(s["dur"] for s in clip_specs) / 60),
                    "name": self.p.get("name", "")}
        tr = self.tr
        vol = tr.get("sfx_volume") or 0.8
        main_sound = self.sound(tr.get("sfx"), vol)
        use_stinger = stinger and (tr.get("stinger") or {}).get("enabled")
        sting_sound = self.sound((tr.get("stinger") or {}).get("sfx"), vol)
        shot_sound = self.sound((tr.get("shot") or {}).get("sfx"), vol)

        segs, joins = [], []

        def add(spec, join):
            if segs:
                joins.append(join)
            segs.append(spec)

        def place(spec, clip, n, n_total, into, sound, stinger_card=None):
            """Add a song, led by its shot card (or the stinger). `into(sound)` makes the join from
            whatever came before; the lead-in card carries the change's sound instead of the song."""
            lead, lead_sound = spec, sound
            if clip.get("shot"):
                lead, lead_sound = self.shot_segment(n, n_total), shot_sound
            elif stinger_card is not None:
                lead, lead_sound = stinger_card, sting_sound
            if segs:
                add(lead, into(lead_sound))
            else:                                        # very first thing in the video
                if lead_sound:
                    lead["sfx"].append(lead_sound | {"at": 0, "offset": 0})
                add(lead, None)
            if lead is not spec:
                add(spec, self.main_join(len(joins), None))

        if intro and self.p["intro"].get("enabled"):
            add(self.card_segment("intro"), None)
        for i, spec in enumerate(clip_specs):
            if i == 0:
                place(spec, main[i], 1, total, lambda snd: self.card_join("intro", snd),
                      main_sound if tr.get("sfx_first") else None)
            else:
                place(spec, main[i], i + 1, total, lambda snd: self.main_join(len(joins), snd), main_sound,
                      self.stinger_segment(i, total) if use_stinger else None)
        if outro and self.p["outro"].get("enabled"):
            add(self.card_segment("outro"), self.card_join("outro", None) if segs else None)
        if bonus_specs:
            gap = float(bcfg.get("gap") or 0)
            if gap > 0 and segs:
                add(self.gap_segment(gap), {"effect": "fade", "d": snap(1.0, self.fps), "sfx": None})
            for k, spec in enumerate(bonus_specs):
                place(spec, extra[k], f"{blabel} {k + 1}", len(extra),
                      lambda snd: self.main_join(len(joins), snd), main_sound)
        return self.finalize(segs, joins)

    def single(self, spec):
        return self.finalize([spec], [])

    def finalize(self, segs, joins):
        fps = self.fps
        for k, j in enumerate(joins):
            if j["d"] > 0:
                limit = floor_snap(min(segs[k]["dur"], segs[k + 1]["dur"]) / 3, fps)
                j["d"] = min(j["d"], limit)
                if j["d"] < 2 / fps:
                    j.update(effect="cut", d=0)
        for k, s in enumerate(segs):
            s["head"] = joins[k - 1]["d"] if k > 0 else 0
            s["tail"] = joins[k]["d"] if k < len(segs) - 1 else 0
            s["entry_sfx"] = joins[k - 1].get("sfx") if k > 0 else None
            if s["entry_sfx"] and s["head"] and s["entry_sfx"]["dur"] <= s["head"] + 0.02:
                s["entry_sfx"] = None      # sound fits entirely inside the transition
            self._expand_countdown(s)
            s.update(W=self.W, H=self.H, fps=fps, enc=self.enc)
            s["hash"] = spec_hash(s)
        for k, j in enumerate(joins):
            j.update(a=segs[k]["hash"], b=segs[k + 1]["hash"], W=self.W, H=self.H, fps=fps, enc=self.enc)
            j["hash"] = spec_hash(j) if j["d"] > 0 else None

        # Timeline + chapters.
        t, starts = 0.0, []
        for s in segs:
            starts.append(max(0.0, t - s["head"]))
            t += s["dur"] - s["head"]
        chapters = [{"start": round(starts[k], 3), "title": s["label"], "kind": s["kind"], "id": s.get("_id")}
                    for k, s in enumerate(segs) if s["kind"] in ("intro", "clip", "outro")]
        for i, c in enumerate(chapters):
            c["end"] = chapters[i + 1]["start"] if i + 1 < len(chapters) else round(t, 3)
        return {"W": self.W, "H": self.H, "fps": fps, "encoder": self.encoder, "quality": self.quality,
                "segments": segs, "joins": joins, "chapters": chapters, "duration": round(t, 3),
                "clip_count": sum(1 for s in segs if s["kind"] == "clip")}

    def _expand_countdown(self, s):
        cd = s.pop("countdown", None)
        if not cd:
            return
        end = s["dur"] - s["tail"]
        earliest = 0.5 if s["kind"] != "clip" else max(0.5, s["dur"] / 2)   # no countdown through a short clip
        first = None
        for k in range(cd["n"], 0, -1):
            t0 = round(end - k, 3)
            if t0 < earliest:
                continue
            first = t0 if first is None else first
            s["overlays"].append({"type": "big_number", "text": str(k), "position": cd["position"],
                                  "bottom_extra": self.bar_h if cd["position"][0] == "b" else 0,
                                  "start": t0, "end": round(t0 + 1, 3), "fade_in": 0.06, "fade_out": 0.3,
                                  **self.style})
            if cd.get("beep"):
                s["sfx"].append(cd["beep"] | {"at": t0, "offset": 0})
        if cd.get("hide_title") and first is not None:
            for o in s["overlays"]:
                if o["type"] == "card_text":
                    o["end"] = first
                    o["fade_out"] = 0.3


# ============================================================================================
# ffmpeg command building
# ============================================================================================

def _even(v):
    return max(2, int(v) // 2 * 2)


def fit_graph(src, out, fill, W, H, sw=0, sh=0):
    """Filtergraph fitting [src] into WxH as [out]: blur-padded, letterboxed or cropped."""
    same = bool(sw and sh) and abs(sw / sh - W / H) < 0.01
    if fill == "crop":
        return f"[{src}]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[{out}]"
    if fill == "black" or same:
        return (f"[{src}]scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[{out}]")
    bw, bh = _even(W / 6), _even(H / 6)
    return (f"[{src}]split=2[{out}a][{out}b];"
            f"[{out}a]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=6:2,"
            f"eq=brightness=-0.12:saturation=1.3,scale={W}:{H},setsar=1[{out}bg];"
            f"[{out}b]scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[{out}fg];"
            f"[{out}bg][{out}fg]overlay=(W-w)/2:(H-h)/2[{out}]")


def concat_line(path):
    return "file '%s'\n" % str(path).replace("\\", "/").replace("'", "'\\''")


def _source(item, need, tmp, name):
    """Input args for a file, played back-to-back when it has to loop.

    Uses a concat list rather than -stream_loop, which can deadlock ffmpeg's scheduler when the
    graph has many inputs.
    """
    if not item.get("loop"):
        return [], item["path"]
    reps = max(2, math.ceil(need / max(float(item.get("src_dur") or 1), 0.1)) + 1)
    lst = tmp / f"{name}.txt"
    lst.write_text("ffconcat version 1.0\n" + concat_line(item["path"]) * reps, "utf-8")
    return ["-f", "concat", "-safe", "0"], str(lst)


def _sound_chain(inp, ev, offset, label):
    return (f"[{inp}:a]{AFMT},atrim=start={offset:.4f},asetpts=PTS-STARTPTS,"
            f"volume={ev['volume']:.3f}{label}")


def _video_input(v, inp, gv, p, W, H, fps, dur, tmp):
    """Adds the input for picture source `v` and a graph producing [{p}fit] at WxH; returns the input index."""
    t_in = f"{dur:.3f}"
    if v["type"] == "file":
        pre, src = _source(v, v.get("ss", 0) + dur + 1, tmp, f"{p}loop")
        pre += ["-ss", f"{v['ss']:.3f}"] if v.get("ss") else []
        vi = inp(*pre, "-t", t_in, "-i", src)
        gv.append(f"[{vi}:v]setpts=PTS-STARTPTS,fps={fps}[{p}src]")
        gv.append(fit_graph(f"{p}src", f"{p}fit", v.get("fill"), W, H, v.get("src_w") or 0, v.get("src_h") or 0))
    elif v["type"] == "image":
        vi = inp("-loop", "1", "-framerate", fps, "-t", t_in, "-i", v["path"])
        gv.append(f"[{vi}:v]fps={fps}[{p}src]")
        gv.append(fit_graph(f"{p}src", f"{p}fit", "crop", W, H))
    elif v["type"] == "gradient":
        vi = inp("-f", "lavfi", "-i",
                 f"gradients=size={W}x{H}:c0={hexc(v['c1'])}:c1={hexc(v['c2'])}:type={v.get('style') or 'linear'}"
                 f":speed={float(v.get('speed') or 0):.4f}:seed=42:rate={fps}:duration={t_in}")
        gv.append(f"[{vi}:v]format=yuv420p,setsar=1[{p}fit]")
    else:
        vi = inp("-f", "lavfi", "-i", f"color=c={hexc(v['c1'])}:size={W}x{H}:rate={fps}:duration={t_in}")
        gv.append(f"[{vi}:v]format=yuv420p,setsar=1[{p}fit]")
    return vi


def segment_command(spec, outs, tmp):
    """ffmpeg args rendering one segment into outs['body'] (+ outs['head'] / outs['tail']).

    Two things keep ffmpeg 8's threaded scheduler from deadlocking at end-of-stream: every input
    lasts exactly as long as the segment (video is padded with tpad instead of reading extra), and
    picture and sound are separate filtergraphs.
    """
    W, H, fps, dur = spec["W"], spec["H"], spec["fps"], spec["dur"]
    head, tail = spec["head"], spec["tail"]
    args, gv, ga = [], [], []
    n_in = 0

    def inp(*a):
        nonlocal n_in
        args.extend(str(x) for x in a)
        n_in += 1
        return n_in - 1

    t_in = f"{dur:.3f}"
    v = spec["video"]
    vi = _video_input(v, inp, gv, "v", W, H, fps, dur, tmp)
    cur = "vfit"
    if spec.get("dim"):
        gv.append(f"[{cur}]drawbox=color=black@{min(0.9, spec['dim']):.2f}:t=fill[vdim]")
        cur = "vdim"

    cv = spec.get("cover")          # trivia: hide the picture for the first `until` seconds
    if cv:
        until, fade = cv["until"], min(float(cv.get("fade") or 0), cv["until"])
        if cv["video"]["type"] == "blur":
            bw, bh = _even(W / 24), _even(H / 24)
            gv.append(f"[{cur}]split=2[vmain][vcsrc]")
            gv.append(f"[vcsrc]scale={bw}:{bh},boxblur=4:3,scale={W}:{H}:flags=bicubic,setsar=1[cfit]")
            cur = "vmain"
        else:
            _video_input(cv["video"], inp, gv, "c", W, H, fps, dur, tmp)
        chain = "[cfit]format=rgba"
        if cv.get("dim"):
            chain += f",drawbox=color=black@{min(0.9, cv['dim']):.2f}:t=fill"
        if fade:
            chain += f",fade=t=out:st={until - fade:.3f}:d={fade:.3f}:alpha=1"
        gv.append(chain + "[cfade]")
        gv.append(f"[{cur}][cfade]overlay=enable='lt(t,{until:.3f})'[vcov]")
        cur = "vcov"

    for k, ov in enumerate(spec["overlays"]):
        img, x, y = cards.render(ov, W, H)
        png = tmp / f"ov{k}.png"
        img.save(png)
        oi = inp("-loop", "1", "-framerate", fps, "-t", t_in, "-i", png)
        chain = f"[{oi}:v]format=rgba"
        s0, s1 = ov.get("start"), ov.get("end")
        if ov.get("fade_in") and s0 is not None:
            chain += f",fade=t=in:st={s0:.3f}:d={ov['fade_in']:.3f}:alpha=1"
        if ov.get("fade_out") and s1 is not None:
            chain += f",fade=t=out:st={max(0.0, s1 - ov['fade_out']):.3f}:d={ov['fade_out']:.3f}:alpha=1"
        gv.append(chain + f"[o{k}]")
        enable = f":enable='between(t,{s0:.3f},{s1:.3f})'" if s0 is not None and s1 is not None else ""
        gv.append(f"[{cur}][o{k}]overlay=x={x}:y={y}{enable}[v{k}]")
        cur = f"v{k}"

    pb = spec.get("progress")
    if pb:
        ph = pb["height"]
        pi = inp("-f", "lavfi", "-i", f"color=c={hexc(pb['color'])}:size={W}x{ph}:rate={fps}:duration={t_in}")
        gv.append(f"[{cur}][{pi}:v]overlay=x='-w+w*t/{dur:.3f}':y={H - ph}[vpb]")
        cur = "vpb"

    vf = ["tpad=stop_mode=clone:stop_duration=1"]      # a source can come up a frame short
    if spec.get("fade_in"):
        vf.append(f"fade=t=in:st=0:d={spec['fade_in']:.3f}")
    if spec.get("fade_out"):
        vf.append(f"fade=t=out:st={dur - spec['fade_out']:.3f}:d={spec['fade_out']:.3f}")
    vf += ["format=yuv420p", f"trim=end={dur:.4f}", "setpts=PTS-STARTPTS"]
    gv.append(f"[{cur}]{','.join(vf)}[vfull]")

    # --- audio
    a = spec["audio"]
    if a["type"] == "src" and v["type"] == "file":
        ain = f"[{vi}:a]"
    elif a["type"] == "file":
        pre, src = _source(a, a.get("ss", 0) + dur + 1, tmp, "aloop")
        pre += ["-ss", f"{a['ss']:.3f}"] if a.get("ss") else []
        ain = f"[{inp(*pre, '-t', t_in, '-i', src)}:a]"
    else:
        ain = f"[{inp('-f', 'lavfi', '-t', t_in, '-i', 'anullsrc=r=48000:cl=stereo')}:a]"
    af = ["aresample=48000:async=1:first_pts=0", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
    if spec.get("normalize"):
        af += ["loudnorm=I=-14:TP=-1.5:LRA=11", "aresample=48000"]
    if float(a.get("volume") or 1) != 1:
        af.append(f"volume={float(a['volume']):.3f}")
    af += ["apad", f"atrim=end={dur:.4f}", "asetpts=PTS-STARTPTS"]
    fade_in = spec.get("fade_in") or (0.015 if head == 0 else 0)      # tiny fades de-click hard cuts
    fade_out = spec.get("fade_out") or a.get("fade_out") or (0.015 if tail == 0 else 0)
    if fade_in:
        af.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out:
        af.append(f"afade=t=out:st={dur - fade_out:.4f}:d={fade_out:.3f}")
    ga.append(f"{ain}{','.join(af)}[a0]")
    acur = "a0"
    events = spec.get("sfx") or []
    if events:
        labels = ""
        for k, ev in enumerate(events):
            si = inp("-i", ev["path"])
            ga.append(_sound_chain(si, ev, ev.get("offset", 0), f",adelay={int(round(ev['at'] * 1000))}:all=1[sx{k}]"))
            labels += f"[sx{k}]"
        ga.append(f"[a0]{labels}amix=inputs={len(events) + 1}:duration=first:dropout_transition=0:normalize=0,"
                  f"{LIMITER}[a1]")
        acur = "a1"

    # --- split into body / head / tail
    parts = [("body", head, dur - tail)]
    if head:
        parts.append(("head", 0, head))
    if tail:
        parts.append(("tail", dur - tail, dur))
    if len(parts) == 1:
        labels = {"body": ("vfull", acur)}
    else:
        n = len(parts)
        gv.append("[vfull]split=%d%s" % (n, "".join(f"[vs{i}]" for i in range(n))))
        ga.append("[%s]asplit=%d%s" % (acur, n, "".join(f"[as{i}]" for i in range(n))))
        labels = {}
        for i, (name, s0, s1) in enumerate(parts):
            gv.append(f"[vs{i}]trim=start={s0:.4f}:end={s1:.4f},setpts=PTS-STARTPTS[v_{name}]")
            ga.append(f"[as{i}]atrim=start={s0:.4f}:end={s1:.4f},asetpts=PTS-STARTPTS[a_{name}]")
            labels[name] = (f"v_{name}", f"a_{name}")
    es = spec.get("entry_sfx")
    if es:
        si = inp("-i", es["path"])
        ga.append(_sound_chain(si, es, head, "[esx]"))
        ga.append(f"[{labels['body'][1]}][esx]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                  f"{LIMITER}[a_bodymix]")
        labels["body"] = (labels["body"][0], "a_bodymix")

    out = [*args, "-filter_complex", ";".join(gv), "-filter_complex", ";".join(ga)]
    for name, _, _ in parts:
        vl, al = labels[name]
        venc = spec["enc"] if name == "body" else ff.scratch_video_args(fps)
        out += ["-map", f"[{vl}]", "-map", f"[{al}]", *venc, *ff.AUDIO_ARGS, str(outs[name])]
    return out


def join_command(j, a_tail, b_head, out):
    """Transition between two segments, from the left tail and right head pieces."""
    fps, d = j["fps"], j["d"]
    args = ["-i", str(a_tail), "-i", str(b_head)]
    gv = [f"[0:v]settb=AVTB,setpts=PTS-STARTPTS,fps={fps},format=yuv420p[va]",
          f"[1:v]settb=AVTB,setpts=PTS-STARTPTS,fps={fps},format=yuv420p[vb]",
          f"[va][vb]xfade=transition={j['effect']}:duration={d:.4f}:offset=0,"
          f"tpad=stop_mode=clone:stop_duration=1,trim=end={d:.4f},setpts=PTS-STARTPTS[v]"]
    ga = [f"[0:a][1:a]acrossfade=d={d:.4f}:c1=tri:c2=tri,apad,atrim=end={d:.4f},asetpts=PTS-STARTPTS[ax]"]
    alabel = "ax"
    if j.get("sfx"):
        args += ["-i", j["sfx"]["path"]]
        ga.append(_sound_chain(2, j["sfx"], 0, f",apad,atrim=end={d:.4f}[s]"))
        ga.append(f"[ax][s]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,{LIMITER}[a]")
        alabel = "a"
    return [*args, "-filter_complex", ";".join(gv), "-filter_complex", ";".join(ga),
            "-map", "[v]", "-map", f"[{alabel}]", *j["enc"], *ff.AUDIO_ARGS, str(out)]


def _meta_escape(s):
    return re.sub(r"([=;#\\\n])", r"\\\1", str(s))


def chapters_text(plan, title):
    lines = [";FFMETADATA1", f"title={_meta_escape(title)}"]
    for c in plan["chapters"]:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(c['start'] * 1000)}", f"END={int(c['end'] * 1000)}",
                  f"title={_meta_escape(c['title'])}"]
    return "\n".join(lines) + "\n"


def tracklist(plan):
    out = []
    for c in plan["chapters"]:
        s = int(c["start"])
        out.append(f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}  {c['title']}")
    return "\n".join(out) + "\n"


# ============================================================================================
# Running a render
# ============================================================================================

class RenderJob:
    def __init__(self, plan, workdir, out_path, workers=3, title="Power Hour", prune=False, project_id=None):
        self.plan = plan
        self.workdir = Path(workdir)
        self.segdir = self.workdir / "segs"
        self.out = Path(out_path)
        self.workers = max(1, int(workers))
        self.title = title
        self.prune = prune
        self.stop = threading.Event()
        self.user_cancel = False
        self.lock = threading.Lock()
        self.fracs, self.weights = {}, {}
        self.total_weight = 1.0
        self.done_units = 0
        self.state = {"status": "running", "progress": 0.0, "stage": "Preparing", "started": time.time(),
                      "eta": None, "output": None, "error": None, "duration": plan["duration"],
                      "segments": len(plan["segments"]), "clips": plan["clip_count"], "encoder": plan["encoder"],
                      "project": project_id}

    # --- bookkeeping ---------------------------------------------------------------------------
    def snapshot(self):
        with self.lock:
            s = dict(self.state)
        s["elapsed"] = time.time() - s["started"]
        return s

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def _progress(self, unit, frac):
        with self.lock:
            self.fracs[unit] = frac
            done = sum(self.weights[u] * f for u, f in self.fracs.items())
            p = min(1.0, done / self.total_weight)
            self.state["progress"] = p
            el = time.time() - self.state["started"]
            if p > 0.01:
                self.state["eta"] = el / p * (1 - p)

    def cancel(self):
        self.user_cancel = True
        self.stop.set()

    def seg_files(self, s):
        f = {"body": self.segdir / f"{s['hash']}.body.mov"}
        if s["head"]:
            f["head"] = self.segdir / f"{s['hash']}.head.mov"
        if s["tail"]:
            f["tail"] = self.segdir / f"{s['hash']}.tail.mov"
        return f

    def join_file(self, j):
        return self.segdir / f"x_{j['hash']}.mov"

    # --- work units ---------------------------------------------------------------------------
    def _render_segment(self, s):
        if self.stop.is_set():
            raise ff.Cancelled()
        files = self.seg_files(s)
        tmpdir = self.workdir / "tmp" / s["hash"]
        tmpdir.mkdir(parents=True, exist_ok=True)
        parts = {k: p.with_name(p.stem + ".part.mov") for k, p in files.items()}
        body_len = s["dur"] - s["head"] - s["tail"]
        cmd = segment_command(s, parts, tmpdir)
        try:
            ff.run(cmd, duration=body_len, on_progress=lambda f: self._progress(s["hash"], f), cancel=self.stop)
        except ff.Stalled:
            log.warning("segment %s stalled, retrying once", s["label"])
            ff.run(cmd, duration=body_len, on_progress=lambda f: self._progress(s["hash"], f), cancel=self.stop)
        for k, p in parts.items():
            os.replace(p, files[k])
        shutil.rmtree(tmpdir, ignore_errors=True)
        self._unit_done()

    def _render_join(self, k):
        j = self.plan["joins"][k]
        a, b = self.plan["segments"][k], self.plan["segments"][k + 1]
        out = self.join_file(j)
        tmp = out.with_name(out.stem + ".part.mov")
        ff.run(join_command(j, self.seg_files(a)["tail"], self.seg_files(b)["head"], tmp), duration=j["d"],
               on_progress=lambda f: self._progress(j["hash"], f), cancel=self.stop)
        os.replace(tmp, out)
        self._unit_done()

    def _unit_done(self):
        with self.lock:
            self.done_units += 1
            self.state["stage"] = f"Rendering pieces ({self.done_units}/{self.total_units})"

    def _parallel(self, fn, items):
        with ThreadPoolExecutor(self.workers) as ex:
            futs = [ex.submit(fn, it) for it in items]
            try:
                for f in as_completed(futs):
                    f.result()
            except BaseException:
                self.stop.set()
                raise

    def run(self):
        plan = self.plan
        self.segdir.mkdir(parents=True, exist_ok=True)
        segs, joins = plan["segments"], plan["joins"]
        todo_segs = [s for s in segs if not all(p.exists() for p in self.seg_files(s).values())]
        seen = set()
        todo_joins = []
        for k, j in enumerate(joins):
            if j["d"] > 0 and not self.join_file(j).exists() and j["hash"] not in seen:
                seen.add(j["hash"])
                todo_joins.append(k)
        for s in todo_segs:
            self.weights[s["hash"]] = s["dur"]
        for k in todo_joins:
            self.weights[joins[k]["hash"]] = joins[k]["d"] * 3 + 1
        self.weights["concat"] = plan["duration"] * 0.004 + 2
        self.total_weight = sum(self.weights.values())
        self.total_units = len(todo_segs) + len(todo_joins)
        cached = len(segs) - len(todo_segs)
        log.info("render: %d segments (%d cached), %d transitions, %.0fs", len(segs), cached, len(todo_joins),
                 plan["duration"])
        self._set(stage=f"Rendering pieces (0/{self.total_units})", cached=cached)

        # Unique segments only (identical stingers share one file).
        uniq = list({s["hash"]: s for s in todo_segs}.values())
        self._parallel(self._render_segment, uniq)
        self._parallel(self._render_join, todo_joins)

        self._set(stage="Joining everything together")
        seq = []
        for k, s in enumerate(segs):
            seq.append(self.seg_files(s)["body"])
            if k < len(joins) and joins[k]["d"] > 0:
                seq.append(self.join_file(joins[k]))
        lst = self.workdir / "concat.txt"
        lst.write_text("ffconcat version 1.0\n" + "".join(concat_line(p) for p in seq), "utf-8")
        meta = self.workdir / "chapters.txt"
        meta.write_text(chapters_text(plan, self.title), "utf-8")
        self.out.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.out.with_name(self.out.stem + ".part.mp4")
        ff.run(["-f", "concat", "-safe", "0", "-i", lst, "-f", "ffmetadata", "-i", meta,
                "-map", "0:v", "-map", "0:a", "-map_metadata", "1", "-map_chapters", "1",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", tmp],
               duration=plan["duration"], on_progress=lambda f: self._progress("concat", f), cancel=self.stop)
        os.replace(tmp, self.out)
        if self.prune:
            keep = {p.name for s in segs for p in self.seg_files(s).values()}
            keep |= {self.join_file(j).name for j in joins if j["d"] > 0}
            for p in self.segdir.iterdir():
                if p.name not in keep:
                    p.unlink(missing_ok=True)
        shutil.rmtree(self.workdir / "tmp", ignore_errors=True)

    def run_safe(self):
        try:
            self.run()
            self._set(status="done", progress=1.0, stage="Done", eta=0, output=str(self.out))
        except ff.Cancelled:
            self._set(status="cancelled" if self.user_cancel else "error", stage="Cancelled",
                      error=None if self.user_cancel else "Render stopped")
        except Exception as e:  # noqa: BLE001
            log.exception("render failed")
            self._set(status="error", stage="Failed", error=str(e)[-1500:])
        finally:
            self._set(finished=time.time())


class RenderManager:
    def __init__(self):
        self.job = None
        self.lock = threading.Lock()

    def busy(self):
        return bool(self.job and self.job.snapshot()["status"] == "running")

    def start(self, job):
        with self.lock:
            if self.busy():
                raise RuntimeError("A render is already running")
            self.job = job
            threading.Thread(target=job.run_safe, daemon=True, name="render").start()

    def status(self):
        return self.job.snapshot() if self.job else {"status": "idle"}

    def cancel(self):
        if self.job:
            self.job.cancel()


# ============================================================================================
# Still-frame previews (no encoding, just ffmpeg for one frame + Pillow)
# ============================================================================================

def _still(v, t, W, H):
    """One frame of picture source `v` at t seconds, fitted to WxH."""
    if v["type"] == "file":
        src_t = v.get("ss", 0) + t
        if v.get("loop") and v.get("src_dur"):
            src_t %= max(float(v["src_dur"]), 0.1)
        g = "[0:v]null[vsrc];" + fit_graph("vsrc", "vout", v.get("fill"), W, H, v.get("src_w") or 0, v.get("src_h") or 0)
        try:
            raw = ff.run_capture(["-ss", f"{src_t:.3f}", "-i", v["path"], "-filter_complex", g, "-map", "[vout]",
                                  "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"], timeout=60)
            return Image.open(BytesIO(raw)).convert("RGB")
        except (ff.FFmpegError, OSError):
            return Image.new("RGB", (W, H), (0, 0, 0))
    if v["type"] == "image":
        return cards.cover(Image.open(v["path"]), W, H)
    if v["type"] == "gradient":
        return cards.gradient(W, H, v["c1"], v["c2"], v.get("style") or "linear")
    return Image.new("RGB", (W, H), cards.rgb(v.get("c1") or "#000000", (0, 0, 0)))


def preview_frame(spec, t):
    W, H = spec["W"], spec["H"]
    t = max(0.0, min(float(t), spec["dur"] - 0.05))
    base = cards.dim(_still(spec["video"], t, W, H), float(spec.get("dim") or 0))
    cv = spec.get("cover")
    if cv and t < cv["until"]:
        if cv["video"]["type"] == "blur":
            small = base.resize((max(2, W // 24), max(2, H // 24)), Image.BILINEAR)
            cover = small.filter(ImageFilter.BoxBlur(2)).resize((W, H), Image.BICUBIC)
        else:
            cover = _still(cv["video"], t, W, H)
        base = cards.dim(cover, float(cv.get("dim") or 0))
    layers = []
    for ov in spec["overlays"]:
        s0, s1 = ov.get("start"), ov.get("end")
        if s0 is not None and not (s0 <= t < s1):
            continue
        layers.append(cards.render(ov, W, H))
    pb = spec.get("progress")
    if pb:
        w = max(1, int(W * t / spec["dur"]))
        layers.append((Image.new("RGBA", (w, pb["height"]), cards.rgba(pb["color"])), 0, H - pb["height"]))
    return cards.compose(base, layers)
