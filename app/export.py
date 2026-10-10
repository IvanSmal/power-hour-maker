"""Export a project as an editable timeline for video editors.

The package is a folder with:
  media/      every song as its own clip (the chosen minute plus a few seconds of handle on each
              side, already fitted to the frame and loudness-matched) plus intro/outro/stinger clips
  graphics/   drink counter, song titles and countdown numbers as transparent full-frame PNGs
  sounds/     the drink-signal sounds
and timelines that reference them:
  <name>.xml   Final Cut Pro 7 XML  -> Adobe Premiere Pro (File > Import), DaVinci Resolve, Avid
  <name>.otio  OpenTimelineIO       -> DaVinci Resolve 18.5+ (File > Import > Timeline), Kdenlive
  <name>.edl   CMX 3600 EDL         -> almost anything (main video track only)
"""
import hashlib
import logging
import os
import shutil
import time
import uuid
import xml.etree.ElementTree as ET
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote

from PIL import Image

from . import cards, ff, render

log = logging.getLogger(__name__)

FORMATS = {
    "premiere": "Premiere Pro / Resolve / Avid (.xml)",
    "otio": "DaVinci Resolve 18.5+ / Kdenlive (.otio)",
    "edl": "EDL - main video track only (.edl)",
}
_TRACK_NAMES = {"lower_third": "Song titles", "counter": "Drink counter", "big_number": "Countdown",
                "card_text": "Trivia text"}
COVER_TRACK = "Trivia cover"


def _premiere_url(p):
    p = Path(p).resolve()
    rest = p.as_posix()[len(p.drive):]
    return "file://localhost/" + quote(p.drive) + quote(rest)


def _safe(name):
    return "".join(c if c.isalnum() or c in " -_.()" else "_" for c in name).strip()[:70] or "clip"


class ExportJob(render.RenderJob):
    def __init__(self, plan, out_dir, workdir, handles=5.0, formats=("premiere", "otio", "edl"),
                 title="Power Hour", project_id=None, workers=3):
        super().__init__(plan, workdir, out_dir, workers=workers, title=title, project_id=project_id)
        self.out_dir = Path(out_dir)
        self.handles = max(0.0, float(handles))
        self.formats = [f for f in formats if f in FORMATS] or ["premiere"]
        self.state["kind"] = "export"

    # --- 1. one clean media file per segment ---------------------------------------------------
    def _media_items(self):
        plan, fps = self.plan, self.plan["fps"]
        items, by_hash = [], {}
        n_stinger = 0
        for k, s in enumerate(plan["segments"]):
            spec = deepcopy(s)
            spec.update(head=0, tail=0, entry_sfx=None)
            before = after = 0.0
            if s["kind"] == "clip":
                spec.update(overlays=[], progress=None, sfx=[])
                spec.pop("cover", None)
                v = spec["video"]
                if not v.get("loop"):
                    before = render.floor_snap(min(self.handles, v.get("ss", 0)), fps)
                    room = float(v.get("src_dur") or 0) - (v.get("ss", 0) + s["dur"]) - 0.1
                    after = render.floor_snap(max(0.0, min(self.handles, room)), fps)
                    v["ss"] = round(v.get("ss", 0) - before, 3)
                spec["dur"] = round(s["dur"] + before + after, 4)
                name = f"{s['n']:03d} {_safe(s.get('_title') or 'song')}.mov"
            elif s["kind"] == "stinger":
                n_stinger += 1
                name = f"S{n_stinger:03d} stinger.mov"
            else:
                name = f"{'000' if s['kind'] == 'intro' else '999'} {s['kind']}.mov"
            h = render.spec_hash(spec)
            if h in by_hash:              # identical stingers share one file
                items.append(dict(by_hash[h], k=k))
                continue
            item = {"k": k, "spec": spec, "hash": h, "file": self.out_dir / "media" / name, "offset": before}
            by_hash[h] = item
            items.append(item)
        return items

    def _cover_items(self):
        """Trivia covers as their own clips, laid over the song for its sound-only part."""
        out, by_hash = {}, {}
        for k, s in enumerate(self.plan["segments"]):
            cv = s.get("cover")
            if s["kind"] != "clip" or not cv:
                continue
            base = {"kind": "cover", "dur": cv["until"], "audio": {"type": "none"}, "normalize": False,
                    "overlays": [], "progress": None, "sfx": [], "fade_in": 0, "fade_out": 0, "head": 0, "tail": 0,
                    "entry_sfx": None, "W": s["W"], "H": s["H"], "fps": s["fps"], "enc": s["enc"]}
            if cv["video"]["type"] == "blur":      # a blurred copy of this song's own picture
                spec = dict(base, video=s["video"], dim=0, cover=dict(cv, fade=0))
                name = f"T{s['n']:03d} blurred cover.mov"
            else:                                  # identical for every song -> one shared file
                spec = dict(base, video=cv["video"], dim=cv.get("dim") or 0)
                name = f"T{len(by_hash) + 1:03d} trivia cover.mov"
            h = render.spec_hash(spec)
            if h not in by_hash:
                by_hash[h] = {"spec": spec, "hash": h, "file": self.out_dir / "media" / name, "offset": 0}
            out[k] = by_hash[h]
        return out

    def _render_media(self, item):
        if self.stop.is_set():
            raise ff.Cancelled()
        spec, out = item["spec"], item["file"]
        tmpdir = self.workdir / "tmp" / item["hash"]
        tmpdir.mkdir(parents=True, exist_ok=True)
        part = out.with_name(out.stem + ".part.mov")
        cmd = render.segment_command(spec, {"body": part}, tmpdir)
        try:
            ff.run(cmd, duration=spec["dur"], on_progress=lambda f: self._progress(item["hash"], f), cancel=self.stop)
        except ff.Stalled:
            ff.run(cmd, duration=spec["dur"], on_progress=lambda f: self._progress(item["hash"], f), cancel=self.stop)
        os.replace(part, out)
        shutil.rmtree(tmpdir, ignore_errors=True)
        self._unit_done()

    # --- 2. the timeline, in frames ---------------------------------------------------------------
    def _layout(self, items):
        plan, F = self.plan, self.plan["fps"]
        segs, joins = plan["segments"], plan["joins"]
        fr = lambda t: int(round(t * F))  # noqa: E731
        starts, t = [], 0.0
        for s in segs:
            starts.append(t - s["head"])
            t += s["dur"] - s["head"]
        total = fr(t)
        cuts, trans = [], []
        for k, j in enumerate(joins):
            half = fr(j["d"]) // 2
            cut = fr(starts[k + 1]) + half
            cuts.append(cut)
            trans.append((cut - half, cut + half, j["effect"]) if half else None)
        bounds = [0] + cuts + [total]
        main = []
        for k, (s, item) in enumerate(zip(segs, items)):
            a, b = bounds[k], bounds[k + 1]
            src_in = a - fr(starts[k]) + fr(item["offset"])
            main.append({"k": k, "item": item, "start": a, "end": b, "in": src_in, "out": src_in + (b - a),
                         "media_dur": fr(item["spec"]["dur"]), "name": s["label"]})
        # graphics: one PNG per distinct overlay image, placed in time
        layers = {COVER_TRACK: [], **{name: [] for name in _TRACK_NAMES.values()}}
        for k, item in self.covers.items():
            a = fr(starts[k])
            layers[COVER_TRACK].append({"file": item["file"], "start": a, "end": a + fr(item["spec"]["dur"])})
        gdir = self.out_dir / "graphics"
        gdir.mkdir(parents=True, exist_ok=True)
        pngs = {}
        W, H = plan["W"], plan["H"]
        for k, s in enumerate(segs):
            if s["kind"] != "clip":
                continue
            for ov in s["overlays"]:
                kind = ov["type"]
                if kind == "card_text" and ov.get("start"):
                    kind = "lower_third"            # a trivia answer shown big is still the song title
                img, x, y = cards.render(ov, W, H)
                canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                canvas.alpha_composite(img, (max(0, x), max(0, y)))
                digest = hashlib.sha1(canvas.tobytes()).hexdigest()[:10]
                if digest not in pngs:
                    label = {"counter": f"counter {ov.get('n', 0):03d}",
                             "lower_third": f"title {s['n']:03d}",
                             "big_number": f"countdown {ov.get('text')}",
                             "card_text": f"trivia {s['n']:03d}"}[kind]
                    p = gdir / f"{label}.png"
                    canvas.save(p)
                    pngs[digest] = p
                if ov["type"] == "counter":
                    a, b = bounds[k], bounds[k + 1]
                else:
                    a, b = fr(starts[k] + ov["start"]), fr(starts[k] + ov["end"])
                layers[_TRACK_NAMES[kind]].append({"file": pngs[digest], "start": a, "end": b})
        # drink-signal sounds on their own track
        sdir = self.out_dir / "sounds"
        sounds = []
        for k, s in enumerate(segs):
            events = []
            if k > 0 and joins[k - 1].get("sfx"):
                events.append((fr(starts[k]), joins[k - 1]["sfx"]))
            if s["kind"] == "clip":
                events += [(fr(starts[k] + e["at"]), e) for e in s.get("sfx") or []]
            for at, ev in events:
                sdir.mkdir(parents=True, exist_ok=True)
                dst = sdir / Path(ev["path"]).name
                if not dst.exists():
                    shutil.copyfile(ev["path"], dst)
                sounds.append({"file": dst, "start": at, "end": at + max(1, fr(ev["dur"])), "volume": ev["volume"]})
        markers = [(fr(c["start"]), c["title"]) for c in plan["chapters"]]
        return {"total": total, "main": main, "trans": trans, "layers": layers, "sounds": sounds, "markers": markers}

    # --- 3. writers --------------------------------------------------------------------------
    def _write_premiere_xml(self, lay, path):
        plan = self.plan
        F, W, H = plan["fps"], plan["W"], plan["H"]
        ids = {"clip": 0, "file": 0}
        files = {}

        def sub(parent, tag, text=None, **attrs):
            el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
            if text is not None:
                el.text = str(text)
            return el

        def rate(parent):
            r = sub(parent, "rate")
            sub(r, "timebase", F)
            sub(r, "ntsc", "FALSE")

        def video_chars(parent):
            sc = sub(parent, "samplecharacteristics")
            rate(sc)
            sub(sc, "width", W)
            sub(sc, "height", H)
            sub(sc, "anamorphic", "FALSE")
            sub(sc, "pixelaspectratio", "square")
            sub(sc, "fielddominance", "none")

        def audio_chars(parent):
            sc = sub(parent, "samplecharacteristics")
            sub(sc, "depth", 16)
            sub(sc, "samplerate", 48000)

        def file_el(parent, p, dur, video=True, audio=True):
            p = Path(p)
            if p in files:
                return sub(parent, "file", id=files[p])
            ids["file"] += 1
            fid = files[p] = f"file-{ids['file']}"
            f = sub(parent, "file", id=fid)
            sub(f, "name", p.name)
            sub(f, "pathurl", _premiere_url(p))
            rate(f)
            sub(f, "duration", dur)
            m = sub(f, "media")
            if video:
                video_chars(sub(m, "video"))
            if audio:
                a = sub(m, "audio")
                audio_chars(a)
                sub(a, "channelcount", 2)
            return f

        def clipitem(track, name, p, start, end, cin, cout, dur, kind, video=True, audio=True, cid=None):
            if cid is None:
                ids["clip"] += 1
                cid = f"clipitem-{ids['clip']}"
            attrs = {"id": cid}
            if kind == "audio":
                attrs["premiereChannelType"] = "stereo"
            c = sub(track, "clipitem", **attrs)
            sub(c, "name", name)
            sub(c, "enabled", "TRUE")
            sub(c, "duration", dur)
            rate(c)
            sub(c, "start", start)
            sub(c, "end", end)
            sub(c, "in", cin)
            sub(c, "out", cout)
            if kind == "video" and not audio:
                sub(c, "alphatype", "straight")
            file_el(c, p, dur, video=video, audio=audio)
            if kind == "audio":
                st = sub(c, "sourcetrack")
                sub(st, "mediatype", "audio")
                sub(st, "trackindex", 1)
            return c

        def transition(track, a, b, kind, effect):
            t = sub(track, "transitionitem")
            sub(t, "start", a)
            sub(t, "end", b)
            sub(t, "alignment", "center")
            rate(t)
            e = sub(t, "effect")
            if kind == "video":
                name = "Dip to Color Dissolve" if effect in ("fadeblack", "fadewhite") else "Cross Dissolve"
                sub(e, "name", name)
                sub(e, "effectid", name)
                sub(e, "effectcategory", "Dissolve")
            else:
                sub(e, "name", "Cross Fade (+3dB)")
                sub(e, "effectid", "KGAudioTransCrossFade3dB")
                sub(e, "effectcategory", "Dissolve")
            sub(e, "effecttype", "transition")
            sub(e, "mediatype", kind)

        root = ET.Element("xmeml", version="4")
        seq = sub(root, "sequence", id="sequence-1")
        sub(seq, "uuid", str(uuid.uuid4()))
        sub(seq, "duration", lay["total"])
        rate(seq)
        sub(seq, "name", self.title)
        media = sub(seq, "media")
        video = sub(media, "video")
        fmt = sub(video, "format")
        video_chars(fmt)

        # V1: songs / cards with transitions; A1 mirrors it with linked stereo audio
        v1 = sub(video, "track")
        vids, aids = [], []
        for c in lay["main"]:
            ids["clip"] += 2
            vids.append(f"clipitem-{ids['clip'] - 1}")
            aids.append(f"clipitem-{ids['clip']}")
        for i, c in enumerate(lay["main"]):
            el = clipitem(v1, c["name"], c["item"]["file"], c["start"], c["end"], c["in"], c["out"], c["media_dur"],
                          "video", cid=vids[i])
            for lid, mt in ((vids[i], "video"), (aids[i], "audio")):
                ln = sub(el, "link")
                sub(ln, "linkclipref", lid)
                sub(ln, "mediatype", mt)
                sub(ln, "trackindex", 1)
                sub(ln, "clipindex", i + 1)
            if i < len(lay["trans"]) and lay["trans"][i]:
                a, b, eff = lay["trans"][i]
                transition(v1, a, b, "video", eff)
        for name, items in lay["layers"].items():
            if not items:
                continue
            tr = sub(video, "track")
            for it in items:
                n = it["end"] - it["start"]
                clipitem(tr, it["file"].stem, it["file"], it["start"], it["end"], 0, n, n, "video", audio=False)
        audio = sub(media, "audio")
        sub(audio, "numOutputChannels", 2)
        afmt = sub(audio, "format")
        audio_chars(afmt)
        a1 = sub(audio, "track")
        for i, c in enumerate(lay["main"]):
            el = clipitem(a1, c["name"], c["item"]["file"], c["start"], c["end"], c["in"], c["out"], c["media_dur"],
                          "audio", cid=aids[i])
            for lid, mt in ((vids[i], "video"), (aids[i], "audio")):
                ln = sub(el, "link")
                sub(ln, "linkclipref", lid)
                sub(ln, "mediatype", mt)
                sub(ln, "trackindex", 1)
                sub(ln, "clipindex", i + 1)
            if i < len(lay["trans"]) and lay["trans"][i]:
                a, b, _ = lay["trans"][i]
                transition(a1, a, b, "audio", None)
        if lay["sounds"]:
            a2 = sub(audio, "track")
            last = -1
            for snd in lay["sounds"]:
                if snd["start"] < last:
                    continue
                n = snd["end"] - snd["start"]
                clipitem(a2, snd["file"].stem, snd["file"], snd["start"], snd["end"], 0, n, n, "audio", video=False)
                last = snd["end"]
        for at, name in lay["markers"]:
            mk = sub(seq, "marker")
            sub(mk, "name", name)
            sub(mk, "comment", "")
            sub(mk, "in", at)
            sub(mk, "out", -1)
        ET.indent(root, "  ")
        xml = ET.tostring(root, encoding="unicode")
        path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + xml + "\n", "utf-8")

    def _otio_timeline(self, lay, main_only=False):
        import opentimelineio as otio
        F = self.plan["fps"]
        RT = lambda f: otio.opentime.RationalTime(f, F)  # noqa: E731
        TR = lambda s, d: otio.opentime.TimeRange(RT(s), RT(d))  # noqa: E731

        def ref(p, dur):
            return otio.schema.ExternalReference(target_url=Path(p).resolve().as_uri(), available_range=TR(0, dur))

        tl = otio.schema.Timeline(name=self.title)
        tl.global_start_time = RT(0)

        def main_track(kind):
            tr = otio.schema.Track(name="Songs" if kind == "video" else "Song audio",
                                   kind=otio.schema.TrackKind.Video if kind == "video" else otio.schema.TrackKind.Audio)
            for i, c in enumerate(lay["main"]):
                clip = otio.schema.Clip(name=c["name"], media_reference=ref(c["item"]["file"], c["media_dur"]),
                                        source_range=TR(c["in"], c["out"] - c["in"]))
                # EDL reel = the number the media file name starts with, so relinking is easy
                clip.metadata["cmx_3600"] = {"reel": c["item"]["file"].name.split(" ")[0][:8].upper()}
                tr.append(clip)
                if i < len(lay["trans"]) and lay["trans"][i]:
                    a, b, eff = lay["trans"][i]
                    tr.append(otio.schema.Transition(name=eff, transition_type=otio.schema.TransitionTypes.SMPTE_Dissolve,
                                                     in_offset=RT((b - a) // 2), out_offset=RT((b - a) - (b - a) // 2)))
            return tr

        def placed(name, kind, items):
            tr = otio.schema.Track(name=name, kind=kind)
            pos = 0
            for it in items:
                if it["start"] < pos:
                    continue
                if it["start"] > pos:
                    tr.append(otio.schema.Gap(source_range=TR(0, it["start"] - pos)))
                n = it["end"] - it["start"]
                tr.append(otio.schema.Clip(name=it["file"].stem, media_reference=ref(it["file"], n),
                                           source_range=TR(0, n)))
                pos = it["end"]
            return tr

        tl.tracks.append(main_track("video"))
        if main_only:
            return tl
        for at, name in lay["markers"]:
            tl.tracks.markers.append(otio.schema.Marker(name=name, marked_range=TR(at, 0)))
        for name, items in lay["layers"].items():
            if items:
                tl.tracks.append(placed(name, otio.schema.TrackKind.Video, items))
        tl.tracks.append(main_track("audio"))
        if lay["sounds"]:
            tl.tracks.append(placed("Drink signal", otio.schema.TrackKind.Audio, lay["sounds"]))
        return tl

    def _write_readme(self, lay):
        mins = lay["total"] / self.plan["fps"] / 60
        text = f"""{self.title} - editable power hour ({mins:.1f} minutes, {self.plan['W']}x{self.plan['H']} @ {self.plan['fps']} fps)

Open one of the timeline files:
  Adobe Premiere Pro : File > Import... > {self.base}.xml
  DaVinci Resolve    : File > Import > Timeline... > {self.base}.otio (or the .xml)
  Anything else      : {self.base}.edl (main video track only)

Tracks
  Video 1  Songs, intro, outro and stingers, with the transitions as dissolves
  Video 2+ Trivia covers (if used), song titles / drink counter / countdown numbers
           (transparent PNGs, full frame)
  Audio 1  Song audio (already loudness-matched)
  Audio 2  Drink-signal sounds

Every song clip has {self.handles:g} s of extra footage on each side, so you can slide or extend it.
Keep this folder together: the timelines point at the files in media/, graphics/ and sounds/.
If you move the folder, use your editor's "relink media" and point it here.
"""
        (self.out_dir / "README.txt").write_text(text, "utf-8")

    # --- run -------------------------------------------------------------------------------------
    def run(self):
        (self.out_dir / "media").mkdir(parents=True, exist_ok=True)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.base = _safe(self.title)
        items = self._media_items()
        self.covers = self._cover_items()
        uniq = list({it["hash"]: it for it in [*items, *self.covers.values()]}.values())
        for it in uniq:
            self.weights[it["hash"]] = it["spec"]["dur"]
        self.weights["timeline"] = 2
        self.total_weight = sum(self.weights.values())
        self.total_units = len(uniq)
        self._set(stage=f"Rendering clips (0/{self.total_units})")
        self._parallel(self._render_media, uniq)
        self._set(stage="Writing timelines")
        lay = self._layout(items)
        written = []
        if "premiere" in self.formats:
            p = self.out_dir / f"{self.base}.xml"
            self._write_premiere_xml(lay, p)
            written.append(p.name)
        if "otio" in self.formats or "edl" in self.formats:
            import opentimelineio as otio
            if "otio" in self.formats:
                p = self.out_dir / f"{self.base}.otio"
                otio.adapters.write_to_file(self._otio_timeline(lay), str(p))
                written.append(p.name)
            if "edl" in self.formats:
                p = self.out_dir / f"{self.base}.edl"
                try:
                    otio.adapters.write_to_file(self._otio_timeline(lay, main_only=True), str(p),
                                                adapter_name="cmx_3600")
                    written.append(p.name)
                except Exception:  # noqa: BLE001 - EDL is a bonus
                    log.exception("EDL export failed")
        self._write_readme(lay)
        self._progress("timeline", 1.0)
        shutil.rmtree(self.workdir / "tmp", ignore_errors=True)
        self._set(written=written)
        log.info("exported %s: %s", self.out_dir, written)

    def run_safe(self):
        super().run_safe()
        if self.state["status"] == "done":
            self._set(output=str(self.out_dir), stage="Export ready")


def new_export_dir(title, base_dir):
    d = Path(base_dir) / f"{_safe(title)} {time.strftime('%Y-%m-%d %H-%M')}"
    d.mkdir(parents=True, exist_ok=True)
    return d
