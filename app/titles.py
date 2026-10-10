"""Turning messy video titles into "Song / Artist" for the on-screen lower third."""
import re

_JUNK_WORDS = (r"official|video|audio|lyrics?|lyric video|visuali[sz]er|hd|hq|4k|1080p|remaster(?:ed)?|"
               r"mv|m/v|explicit|clean|music|clip|version|color coded|letra|subtitulad[oa]|360|vevo|uncensored")
_BRACKETS = re.compile(r"\s*[\(\[\{【][^\)\]\}】]*?\b(?:" + _JUNK_WORDS + r")\b[^\)\]\}】]*[\)\]\}】]",
                       re.I)
_TRAIL = re.compile(r"\s*(?:[|/]\s*|\s-\s)(?:" + _JUNK_WORDS + r")(?:\s+(?:" + _JUNK_WORDS + r"))*\s*$", re.I)
_DASH = re.compile(r"\s+[-–—~|]\s+")


_DECOR = re.compile(r"[*_#~★☆•♪♫❤♥✨\U0001F300-\U0001FAFF]+")
_BARE_TRAIL = re.compile(r"\s+(?:M/?V|official\s+(?:music\s+)?video|lyric\s+video|lyrics)\s*$", re.I)


def clean(title):
    t = _BRACKETS.sub("", title or "")
    t = _TRAIL.sub("", t)
    t = _BARE_TRAIL.sub("", t)
    t = _DECOR.sub(" ", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" -|~")
    return t or (title or "").strip()


def _artist_from_channel(channel):
    c = (channel or "").strip()
    c = re.sub(r"\s*-\s*Topic$", "", c)
    c = re.sub(r"(?i)\s*(VEVO|official|music)$", "", c).strip()
    if " " not in c:
        c = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", c)   # "TheKillers" -> "The Killers"
    return c if c.lower() not in ("local file", "") else ""


def split(title, channel=""):
    """(song, artist) best guess."""
    t = clean(title)
    parts = _DASH.split(t, maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1]:
        artist, song = parts
        return song.strip(" \"'“”"), artist.strip()
    return t.strip(" \"'“”"), _artist_from_channel(channel)


def display(clip, media=None):
    """What the lower third shows for a clip, honouring the user's overrides."""
    title = (media or {}).get("title") or clip.get("title") or ""
    channel = (media or {}).get("channel") or clip.get("channel") or ""
    song, artist = split(title, channel)
    return (clip.get("display_title") or song).strip(), (clip.get("display_artist") or artist).strip()
