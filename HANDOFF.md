# Power Hour Maker — handoff notes

Written 2026-10-10 (rounds 1-3 of features). Everything lives in
`F:\Documents\Claude\power hour maker`. Read this before continuing work.

## What it is

A personal Windows desktop app that builds power-hour drinking-game videos: search/download
videos (yt-dlp), pick one minute per song (YouTube "Most Replayed" heatmap, else loudest minute),
add intro/outro cards, transitions, drink-signal sounds and overlays, render one MP4 with chapters.

Stack: portable Python 3.12 (uv-managed, in `runtime\`, venv in `.venv\`), Flask backend serving a
vanilla HTML/CSS/JS UI shown in a pywebview (WebView2) window; FFmpeg 8.1 + Deno + uv in `bin\`.
Nothing is installed system-wide. `setup.ps1` bootstraps everything (idempotent).

Launch: `Power Hour Maker.lnk` / `Power Hour Maker.bat` (runs `pythonw -m app`).
Dev server for the browser pane: `.claude\launch.json` → `python -m app --serve --port 8765 --verbose`.

## File map

| Path | Role |
| --- | --- |
| `setup.ps1` | Downloads uv, FFmpeg (pinned to **8.1**, see gotchas), Deno; creates venv; installs `requirements.txt`; makes shortcut. `-Update` refreshes yt-dlp, `-DesktopShortcut`. |
| `app\main.py` | Entry point. **Single-instance**: writes `data\server.json` {port,pid}; a second launch / browser mode / Claude attach to a running backend (`/api/ping`). Flags: `--browser`, `--serve`, `--idle-exit N`, `--port`, `--verbose`. After the window closes it keeps serving while busy, then exits after 20 s idle. |
| `app\server.py` | Flask API (all `/api/*` routes) + static UI. Globals: `settings`, `media`, `projects`, `renders`. |
| `app\media.py` | `MediaStore`: search/resolve (yt-dlp), downloads with progress + 3-try retry for transient errors, analysis, long-video sections, thumbnails, local files/uploads. Cache in `data\cache\<key>.{mp4,json}`. |
| `app\analysis.py` | Loudness profile, heatmap peak (ignores start spike), loudest window, `auto_start`, `suggest_section`. |
| `app\render.py` | `PlanBuilder` (project → segment/join specs), ffmpeg command builders, `RenderJob`/`RenderManager`, still-frame previews. Segment cache keyed by spec hash. |
| `app\export.py` | `ExportJob` (subclass of RenderJob): editor export package (Premiere FCP7 XML written by hand, `.otio` and `.edl` via OpenTimelineIO). |
| `app\mcp_server.py` | **Claude integration**: MCP server (mcp SDK **2.x**, `MCPServer`), 26 tools, thin HTTP client to the backend; auto-spawns a hidden backend (`--serve --idle-exit 900`) if none runs. |
| `power_hour_mcp.py` | Root-level launcher so the MCP server works from any cwd. |
| `app\install_claude.py` + `install-claude.ps1` | Registers MCP server + skill with Claude Code (`~/.claude.json` mcpServers + `~/.claude/skills/power-hour`) and Claude Desktop (`%APPDATA%\Claude\claude_desktop_config.json`). Backs up configs. `--uninstall`. **Not run yet — needs the user's OK** (global config change). |
| `.mcp.json` | Project-scoped MCP registration (works for Claude Code sessions opened in this folder). |
| `.claude\skills\power-hour\SKILL.md` | Skill describing the themed-power-hour workflow for Claude. |
| `app\cards.py` | Pillow graphics: drink counter, lower third, big countdown numbers, card text; font fallback for CJK/Hangul/etc. |
| `app\sfx.py` | Synthesized sounds via ffmpeg `aevalsrc` (air horn, buzzer, gong, ... + new: thunder, organ, ghost, bell, sleigh, party_horn, scratch, slide_whistle). Custom sounds = `file:<asset>`. |
| `app\themes.py` | Theme presets (classic, halloween, christmas, stpatricks, birthday, newyear, neon80s, centurion) = partial project patches. |
| `app\titles.py` | Clean YouTube titles → (song, artist). |
| `app\project.py` | Project JSON store (`data\projects\<id>\project.json` + `assets\`), defaults, app settings (`data\settings.json`). |
| `app\ff.py` | ffmpeg run wrapper (progress, cancel, **stall watchdog 45 s → `Stalled`**), probe, encoder detection, quality presets. |
| `app\paths.py` | Paths; **prepends `bin\` to PATH** (yt-dlp needs it). |
| `web\index.html`, `web\app.js`, `web\style.css` | UI. Tabs: Songs, Intro & Outro, Transitions, Look, Render. Trimmer modal, section-picker modal, settings/projects/bulk/folder modals. |
| `README.md` | User-facing docs (up to date with rounds 1-3). |

## Round 1 (done, tested end-to-end)

Search/paste links (YouTube search; any yt-dlp site via link; playlists expand), bulk add, local
files, Most-Replayed auto minute, trimmer (drag box on heatmap/loudness timeline, keyboard), short
videos play-as-is/loop, 60/100/120/custom length, intro/outro editor (gradient/color/image/video bg,
music/sfx, countdown), 30 xfade transitions + random, drink sounds, DRINK! stinger, overlays
(counter, lower third, progress bar, last-seconds countdown), blur fill, loudness normalisation,
vertical/square output, chapters + tracklist, quick preview, segment cache (re-render only changed
songs), NVENC. Verified: frame-exact output, A/V lengths equal, loudness −13..−15 LUFS, ~12×
realtime at 1080p with NVENC (60-min power hour ≈ 5 min).

## Round 2 (user's request) — status

1. **Export to Premiere/Resolve-like editors** — `app\export.py`, `/api/export`, Render-tab panel.
   - Package in `data\exports\<name> <date>\`: `media\` (each song = clean clip with ±N s handles,
     fitted + loudness-matched; intro/outro/stingers fully rendered), `graphics\` (full-frame
     transparent PNGs: counters, titles, countdown digits), `sounds\`, `<name>.xml` (FCP7 XML,
     Premiere-style, own writer: sequence format, linked stereo clips `premiereChannelType`,
     `Cross Dissolve`/`Dip to Color Dissolve`, audio `KGAudioTransCrossFade3dB`, sequence markers),
     `<name>.otio` (Resolve 18.5+), `<name>.edl` (V1 only, reels = file-number prefix), README.txt.
   - Tested headless: works, XML parses back via OTIO's fcp_xml reader with correct tracks and
     duration. **Not tested in real Premiere/Resolve** (not installed). EDL reel-name patch untested.
2. **Claude integration** — MCP server tested through a real MCP client (stdio): status,
   create_project(theme), add_songs, wait_for_downloads, list_songs, song_details, set_song,
   preview_image (returns JPEG), set_background_video, errors via `ToolError` reach the model.
   Not yet tested: the `power_hour_mcp.py` launcher from a different cwd, choose_section,
   export_for_editor, render via MCP, open_app. Registration script not run (ask user).
3. **Local folder of videos** — `/api/clips/folder` (+ `/api/clips/local` accepts folders,
   `recursive`), UI "📁 Add folder" modal (pywebview folder dialog via `JsApi.pick_folder`, or type a
   path in browser mode). Backend/UI **not yet exercised**.
4. **Large files / long videos** — anything longer than `settings.long_minutes` (15) gets media
   status `"long"` and is NOT downloaded/analysed whole. User picks a section (default
   `section_minutes` = 10; 5/10/15) → derived media key `<parent>~<start>-<end>`.
   - Online: yt-dlp's partial download (ffmpeg on googlevideo URLs) is unusably slow/hangs, so the
     original is downloaded once with the normal fast downloader (~50 MB/s here), kept for
     re-picks (second section = 1 s), and the section is stream-copied out.
   - Local (movies, MKV/HEVC/AC3...): section re-encoded to H.264/AAC ≤1080p (NVENC) so the
     preview player can play it. Original untouched.
   - Section picker shows a 30-thumb filmstrip: YouTube storyboards (no download needed) or ffmpeg
     frames for local files; heatmap overlay; draggable box; big hover preview; "Suggested" =
     centred on most-replayed peak. Trimmer shows "this is X–Y of a longer video" + Change section.
   - `/api/clips/auto_sections` + "✂ Suggested sections" button for all long songs at once.
   - Backend tested (online 56-min video, local 21-min clock video — cut is frame-accurate).
     **UI (section picker, buttons) written but not yet tested in the browser.**
5. Extras added this round: theme presets (Look tab "Theme preset" + `/api/themes/apply`), 8 new
   sounds, download retries (`_PERMANENT` error list = no retry), settings for long videos and a
   "Claude integration" section in the Settings modal, render panel shows Export progress.

## Round 3 — trivia mode (done, tested)

User request: a "trivia" mode in the Look tab: sound only first, then the video without the title,
then the title. Settings in `overlays.trivia` {enabled, audio_seconds 20, video_seconds 20, text,
subtext ({n},{total}), reveal "lower_third"|"big", bg {type gradient|color|image|media|blur, ...}}.
- `render.PlanBuilder.trivia_phases(dur)` → (end of sound-only part, reveal time); squeezed
  proportionally when the clip is shorter (keeps ≥3 s of answer). JS `triviaPhases` mirrors it for
  the phase bar in the Look tab.
- Clip spec gets `cover` {video, until, dim, fade}: `segment_command` overlays the cover on the
  picture until `until` (alpha fade-out 0.4 s); type "blur" splits the clip's own picture and
  blurs it heavily (easier variant). `_video_input()` is the shared picture-source builder.
  Normal title overlay is replaced by the reveal (lower third or big centred card_text) that stays
  until the song ends. "GUESS THE SONG" card_text (with `sub_scale`) shows during the cover.
- `preview_frame` draws the cover too; `_still()` is the shared frame grabber.
- Export: covers are their own clips (`media/T### ...`), shared when identical (non-blur), on a
  "Trivia cover" track under the overlay tracks; trivia text on "Trivia text"; big reveal on
  "Song titles".
- MCP: `update_settings` doc lists trivia; `set_background_video("trivia", ...)`; skill has a tip.
- Tested: headless renders (gradient + blur covers, both reveal styles), frame previews, quick
  preview, editor export (parses back via OTIO with the new tracks), MCP via `power_hour_mcp.py`
  from another cwd (trivia settings, preview_image, choose_section, render).

Also this round: browser-tested the round-2 UI (trimmer excerpt line, section picker incl.
Suggested + Use, Add folder modal, settings modal, export panel + run). Fixes: right-hand preview
column now actually sticky (`.two-col > .col:last-child { align-self: stretch }`); section picker
opened from the trimmer returns to it; "Delete downloads no project uses" now also deletes
sections cut from local files (they're our copies in the cache; it used to skip anything `local`).

Final test (user's request): made two full power hours through the MCP tools, output moved to the
user's Videos folder (`F:\Videos`): "Horror Movie Power Hour" (60 songs from horror films, artist
line = "artist · movie (year)") and "21st Century Punk Power Hour (Trivia)" (trivia mode, turntable
video cover, answers "artist · year"). Fixes found on the way:
- `analysis.heat_peak`: the heatmap stays high for 20-40 s after the start spike (viewers who just
  started); an early pick no higher than the opening now yields to a later peak >= 85 % as strong
  (genuine early peaks that rise above the opening are kept). Fixed ~6 of 120 songs (video intros).
- `analysis.skip_silence`: automatic starts skip dead air (< -50 dBFS, max 15 s) - e.g. silent
  video openings (blink-182 "The Rock Show" had 9 s).
- `cards.safe_text`: punctuation a font can't draw (Stencil/Broadway/Cooper Black lack "·") is
  swapped for ASCII instead of rendering as boxes (detected by comparing with the .notdef glyph).

GitHub: public repo `IvanSmal/power-hour-maker` (made public by the user; gh CLI installed via
winget, logged in as IvanSmal; repo-local git identity uses the GitHub no-reply address).

## Round 4 — shot songs, bonus songs, 30-min preset (done, tested)

- Clips have `shot` and `bonus` flags (`/api/clips/flags` {ids, shot?, bonus?}; MCP `set_song(shot=,
  bonus=)`). `ProjectStore._partition()` keeps bonus songs after the main songs (add/reorder/flags);
  a song that changes group goes to the end of its new group (`set_flags`; app.js `setFlags` mirrors it).
- Plan (`PlanBuilder.build`): main songs as before; a shot song is led by `shot_segment` (the shot
  card carries the change's sound, replaces the stinger there); after the outro: `gap_segment`
  (`bonus.gap` s of black, joined with a 1 s fade) then the bonus songs, counter label `bonus.label`
  + number, chapters "Bonus 1"... (no titles - they're secret). `{count}`/`{minutes}`/target ignore
  bonus songs. `_flash_card()` is shared by stinger and shot cards; `cards.shot_glass` draws the
  glass (4x supersampled). Spec bookkeeping: `_tag` ("012" / "B01") names export files/graphics.
- UI: 🥃/🎁 toggles per row, "🎁 Bonus songs" header in the playlist (gap + label inputs, drop target),
  "🥃 Shot songs" toolbar modal (every Nth / random / clear), shot card panel + preview in Transitions,
  stats pills. Length presets now 30/60/100/120.

## Immediate next steps

1. Real window mode with the single-instance `main.py` is still untested (launch twice → second
   attaches; close window → backend exits after idle; MCP attaches to the window's backend).
2. Ask the user whether to run `install-claude.ps1` (modifies `~/.claude.json` and Claude Desktop
   config; backups are written). Sessions opened in this folder already get the tools via `.mcp.json`.
3. Not tested in real Premiere/Resolve (not installed). Theme "Apply" button uses `confirm()`
   (only tested through the API).
4. Ideas: show trivia phase marks in the trimmer; option to start trivia clips at the song's start
   instead of the most-replayed chorus (harder quiz).
5. Before finishing: `deno lint --rules-exclude=no-window,no-window-prefix web/app.js`,
   `python -m py_compile app/*.py`.

## Test data

Cleaned up in round 3. "Saturday Power Hour" is the user's demo project (render in `data\output\`).
Scratch scripts of each session live in that session's scratchpad (`%TEMP%\claude\F--Documents-
Claude-power-hour-maker\<session>\scratchpad`), e.g. `trivia_test.py`, `mcp_test2.py`.

## Gotchas learned (don't re-discover these)

- **FFmpeg 9.x NVENC needs NVIDIA driver 610+**; this machine's driver exposes NVENC API 13.0 →
  setup pins the 8.1 series (BtbN `ffmpeg-n8.1-latest-win64-gpl-shared-8.1.zip`).
- **FFmpeg 8 scheduler deadlocks at end-of-stream** with many inputs. Fixes in `segment_command`:
  inputs last exactly the segment length (`-t dur`, video padded with `tpad`), audio and video are
  separate `-filter_complex` graphs, and looping uses a concat list instead of `-stream_loop`.
  Plus the 45 s stall watchdog + one retry. Stress-tested 180 parallel runs clean.
- Intermediates are `.mov` (h264, `-bf 0`, PCM) so the final concat can stream-copy with exact
  timestamps (mkv's ms timestamps caused 33/34 ms jitter).
- yt-dlp now needs a JS runtime (Deno) for YouTube: `js_runtimes={"deno": {"path": ...}}`.
- yt-dlp partial-download check ignores `ffmpeg_location` → `bin\` is put on PATH.
- vimeo.com pages need login in yt-dlp; `player.vimeo.com/video/<id>[?h=hash]` doesn't →
  `media.normalize_url`.
- YouTube 403s are intermittent → retries. Google/Bilibili/Niconico search keys return junk → only
  YouTube search is offered.
- Heatmap's first bucket is always the max (people start watching) → ignore first max(10 s, 5%).
- MCP SDK is 2.x: `from mcp.server.mcpserver import MCPServer, Image`; raise
  `mcp.server.mcpserver.exceptions.ToolError` (other exceptions hide their message);
  client results use `is_error`, `mime_type`.
- pythonw has no stdout/stderr → `main._setup_logging` redirects; yt-dlp always gets a logger.
- Bash tool here chokes on long heredocs containing `'''` → write patch scripts to the scratchpad
  with the Write tool and run them with the venv python. `rm -rf` with globs after `cd` is blocked
  by a safety check → use explicit absolute paths.
- Browser pane coordinates are in the 800×508 screenshot frame when the viewport is emulated at
  1480×940; take a screenshot before coordinate clicks in a batch.

## Performance reference (this PC: i7-14700F, RTX 5060 Ti)

1080p render ≈ 12× realtime with NVENC p4 (CPU x264 ≈ 8.5×, CPU-bound regardless of workers).
"Good" quality = NVENC cq 27 / x264 crf 23 (~SSIM 0.985). Downloads ~40–70 MB/s.
