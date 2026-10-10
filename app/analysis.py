"""Picking the best minute of a video: YouTube "Most replayed" heatmap first, loudness second."""
import array
import math

from . import ff

LOUD_RATE = 4000  # Hz; plenty for a per-second loudness envelope


def loudness_profile(path):
    """Per-second RMS loudness of the audio track in dBFS (list of floats)."""
    raw = ff.run_capture(["-i", str(path), "-vn", "-ac", "1", "-ar", LOUD_RATE, "-f", "s16le", "-"])
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    out = []
    full = 32768.0 ** 2
    for i in range(0, len(samples), LOUD_RATE):
        chunk = samples[i:i + LOUD_RATE]
        if len(chunk) < LOUD_RATE // 4:
            break
        ms = sum(x * x for x in chunk) / len(chunk)
        out.append(round(10 * math.log10(ms / full + 1e-10), 1))
    return out


def heat_peak(heatmap, duration):
    """Time (s) of the most replayed moment, ignoring the start-of-video spike everyone has."""
    if not heatmap or not duration:
        return None
    pts = [((h[0] + h[1]) / 2, h[2]) for h in heatmap]
    n, k = len(pts), 2
    smooth = []
    for i in range(n):
        win = pts[max(0, i - k):i + k + 1]
        smooth.append(sum(v for _, v in win) / len(win))
    lo = max(10.0, duration * 0.05)
    hi = duration - max(5.0, duration * 0.03)
    cands = [i for i, (t, _) in enumerate(pts) if lo <= t <= hi]
    if not cands:
        return None
    best = max(cands, key=lambda i: smooth[i])
    # Viewers who just started watching keep the first stretch high for a while after the spike.
    # An early pick no higher than the opening is that tail, not a highlight: prefer a later peak
    # that's nearly as strong (genuine early peaks rise above the opening and are kept).
    opening = max((smooth[i] for i, (t, _) in enumerate(pts) if t < lo), default=0.0)
    late = [i for i in cands if pts[i][0] >= duration * 0.2]
    if late and smooth[best] <= opening:
        later = max(late, key=lambda i: smooth[i])
        if smooth[later] >= 0.85 * smooth[best]:
            best = later
    return pts[best][0]


def loudest_start(loudness, length, duration):
    """Start of the `length`-second window with the most acoustic energy."""
    w = int(round(length))
    n = min(len(loudness), int(duration))
    if n <= w:
        return 0.0
    power = [10 ** (d / 10) for d in loudness[:n]]
    acc = [0.0]
    for p in power:
        acc.append(acc[-1] + p)
    best_s, best_v = 0, -1.0
    for s in range(0, n - w + 1):
        v = acc[s + w] - acc[s]
        if v > best_v:
            best_s, best_v = s, v
    return float(best_s)


def clamp_start(start, length, duration):
    return max(0.0, min(float(start), max(0.0, duration - length - 0.05)))


SILENCE_DB = -50.0


def skip_silence(start, loudness, length, duration, limit=15):
    """Move a start past dead air (e.g. a music video's silent opening), at most `limit` seconds."""
    s = int(start)
    end = min(len(loudness or []), s + limit)
    while s < end and loudness[s] < SILENCE_DB:
        s += 1
    return clamp_start(max(float(start), s), length, duration)


def suggestions(media, length):
    """All automatic start candidates for a clip of `length` seconds."""
    dur = media.get("duration") or 0
    out = {}
    if dur <= length + 0.05:
        return out
    loud = media.get("loudness")
    peak = heat_peak(media.get("heatmap"), dur)
    if peak is not None:
        out["heatmap"] = round(skip_silence(clamp_start(peak - length / 2, length, dur), loud, length, dur), 2)
    if loud:
        out["loudness"] = round(clamp_start(loudest_start(loud, length, dur), length, dur), 2)
    out["default"] = round(skip_silence(clamp_start(dur * 0.25, length, dur), loud, length, dur), 2)
    return out


def auto_start(media, length):
    """(start, source) where source says which heuristic chose it."""
    dur = media.get("duration") or 0
    if dur <= length + 0.05:
        return 0.0, "full"
    s = suggestions(media, length)
    for key in ("heatmap", "loudness", "default"):
        if key in s:
            return s[key], key
    return 0.0, "default"


def suggest_section(media, length):
    """Start of a `length`-second section of a long video, centred on its most replayed moment."""
    dur = float(media.get("duration") or 0)
    if dur <= length:
        return 0.0
    peak = heat_peak(media.get("heatmap"), dur)
    if peak is None:
        return None
    return round(max(0.0, min(peak - length / 2, dur - length)), 1)
