# 🍺 Power Hour Maker

Builds power hour drinking-game videos: one minute of each song, a drink signal at every change,
intro + outro cards, and a single finished MP4 with chapters.

## Launch

Double-click **Power Hour Maker** (the shortcut) or **Power Hour Maker.bat**.

Everything lives in this folder. The first run of `setup.ps1` (the .bat runs it automatically if
needed) downloads a private Python, FFmpeg and Deno into `runtime\`, `bin\` and `.venv\`; nothing
is installed system-wide. Run `setup.ps1 -DesktopShortcut` to also put a shortcut on the desktop.

`.venv\Scripts\python.exe -m app --browser` opens the UI in your normal browser instead of the app window.

## Using it

**Songs**
- Pick the length (60 / 100 / 120 minutes or anything custom) and how long each song plays.
- Search YouTube, or paste a link from YouTube, Vimeo, Dailymotion, TikTok, Twitch clips and
  ~1800 other sites. A playlist link adds every video in it.
- **Bulk add**: paste a list of `Artist - Song` lines and/or links; the top hit for each is downloaded.
- **Add files** / **📁 Add folder**: use videos already on your computer (a folder adds every video
  in it, sorted by name; subfolders optional). Files stay where they are.
- **Long videos** (movies, concerts, compilations, anything over 15 minutes - Settings → *Long videos*)
  aren't downloaded and analysed whole. Pick a 5/10/15-minute section on a filmstrip (YouTube's
  preview pictures, so nothing downloads until you confirm), then the minute inside it as usual.
  *✂ Suggested sections* picks the most-replayed part of every long video at once.
- Every song gets an automatic minute: YouTube's **Most Replayed** peak (centred), or the loudest
  minute when a site has no replay data.
- Click a song to open the trimmer: drag the gold box over the heatmap, double-click to centre it,
  ←/→ nudge 1 s (Shift = 5 s), Space plays the minute, Enter jumps to the next song. You can also fix
  the on-screen title/artist or swap in a different version of the video.
- Videos shorter than a minute are fine: they play as-is, or loop to fill the slot
  (Songs → *Short videos*).
- Drag ⋮⋮ to reorder, or shuffle.

**Intro & Outro**: title card with gradient/colour/image/video background, music or a sound
effect, optional 3-2-1 countdown, and the transition into the first song / out of the last.

**Transitions**: 30 effects (or a random mix), the drink signal sound (air horn, buzzer, gong,
glass clink, ... or your own file), and an optional "DRINK!" stinger card or clip between songs.

**Look**: theme presets (Halloween, Christmas, St Patrick's, birthday, New Year, neon 80s,
Centurion...), resolution (up to 4K, or vertical/square), how odd-shaped videos fill the frame,
the drink counter, song titles, progress bar, last-seconds countdown, fonts and colours.
The preview on the right is a real rendered frame.

**🎯 Trivia mode** (Look tab): turns the power hour into a guess-the-song game. Each song starts
with sound only behind a cover ("GUESS THE SONG"; gradient, colour, image, a video, or a heavily
blurred copy of the song's own video as an easier hint), then the video plays without its title,
then the answer appears and stays until the song ends. Default 20 s + 20 s + the rest; the times
shrink proportionally for shorter songs. The answer can be the usual title box or big and centred.

**Render**: *Quick preview* renders 3 songs at 480p in a few seconds. *Render full video* writes
`data\output\<name>.mp4` with chapter markers, plus a `.txt` tracklist (paste it into a YouTube
description for chapters). Re-rendering after a small change only redoes the songs that changed.
On an NVIDIA GPU a 60-minute power hour renders in roughly 5 minutes.

**Export for editing** (Render tab): writes a folder in `data\exports\` to finish the video in an
editor - every song as its own clip with a few spare seconds on each side, intro/outro/stingers,
the overlays as transparent PNG layers, trivia covers on their own track, drink sounds on their own
audio track, and timelines for **Premiere Pro** (`.xml`, File → Import), **DaVinci Resolve**
(`.otio` or `.xml`) and anything else (`.edl`, main track only).

## Let Claude make one

Power Hour Maker has a Claude integration (an MCP server): ask Claude Code or Claude Desktop for
"a Halloween power hour" or "a 90s music trivia hour" and it picks the songs, downloads them, styles
the video, checks preview frames and renders it. Claude Code sessions opened in this folder have it
automatically (`.mcp.json`); run `install-claude.ps1` once to make it available everywhere (it adds
the server to `~/.claude.json` and Claude Desktop's config, with backups; `-Uninstall` removes it).

## When something breaks

- **Downloads start failing**: sites change often. Settings → *Update yt-dlp*, then restart.
- **"Sign in to confirm you're not a bot"** or age-restricted videos: Settings → *Browser cookies*
  → Firefox (Chrome's cookies are locked down on Windows).
- **Private/unlisted Vimeo videos** need a link that includes the share hash.
- Logs: `data\app.log`.

## Folder layout

| Path | What |
| --- | --- |
| `app\` | Python backend: downloads (yt-dlp), analysis, rendering (FFmpeg), local API (Flask) |
| `web\` | The UI (plain HTML/CSS/JS) |
| `data\cache\` | Downloaded videos, shared by all projects |
| `data\projects\` | Your projects (JSON + uploaded images/sounds) |
| `data\output\` | Finished videos |
| `data\exports\` | Editor export packages |
| `data\work\` | Render cache (safe to delete, Settings → *Clear render cache*) |
| `bin\`, `runtime\`, `.venv\` | Portable FFmpeg 8.1, Deno, uv, Python 3.12 |

FFmpeg is pinned to the 8.1 series because 9.x's NVENC needs NVIDIA driver 610+.
