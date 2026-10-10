---
name: power-hour
description: Build power hour / Centurion drinking-game videos with the Power Hour Maker app (MCP server "power-hour"). Use when the user asks for a power hour, a themed drinking-game music video, a "one minute per song" mix, or wants to edit/render/export one of their Power Hour Maker projects.
---

# Making a power hour with Power Hour Maker

The `power-hour` MCP server drives the user's Power Hour Maker app. If the app window is open, the
user watches you work live; `open_app()` opens it.

## Workflow

1. **Plan the playlist first.** A power hour is `minutes / seconds_per_song` songs (60 → 60 songs,
   Centurion = 100). Pick ~10% extra so failures can be dropped. Good picks are well-known,
   high-energy, singable, varied in era and tempo, and fit the theme. No duplicates, no 10-minute
   prog epics unless asked. For a theme (Halloween, Christmas, 2000s, a specific artist...), mix
   obvious anthems with a few deep cuts.
2. `create_project(name, minutes, theme=...)` - check `options()` for presets
   (halloween, christmas, stpatricks, birthday, newyear, neon80s, centurion, classic).
3. `add_songs([...], search_suffix="official video")` in batches of 20-30, as "Artist - Title".
   Use "lyrics" instead of "official video" when official videos are likely blocked (e.g. some
   major-label songs).
4. While it downloads, style it: intro/outro titles and subtitles, colours, font, sounds via
   `update_settings()`; maybe `set_background_video("intro", "spooky fog loop 4k")`.
   Check the look with `preview_image("intro")` and `preview_image("song", seconds=3)` (title +
   counter) / `seconds=57` (countdown) - actually look at the images and fix clashes.
5. `wait_for_downloads()`. For each item in `needs_attention`:
   - `failed` / `not found` → `replace_song(n, "Artist - Title lyrics")` or another version,
     or `remove_songs` and add a spare.
   - `needs section` (movies, compilations, concerts) → `choose_section(n)` uses the most
     replayed part; pass `start_seconds` if the user named a scene.
   Then `list_songs()` and sanity-check titles: wrong artist, covers, live versions, reaction
   videos, "1 hour loop" uploads → replace them.
6. Minutes are picked automatically (YouTube "most replayed" peak, else loudest minute). Only
   fine-tune with `song_details()` + `set_song(start_seconds=...)` when asked, or when the pick
   is clearly odd (e.g. starts at 0:00 on a long intro).
7. Fix on-screen names when the parsed title is messy: `set_song(n, title=..., artist=...)`.
8. `render()` then `render_status(wait_seconds=600)` (a 60-minute 1080p render takes ~5-10
   minutes). Or `quick_preview()` first. For editing in Premiere/Resolve: `export_for_editor()`.

## Tips

- **Trivia / guess-the-song** ("music quiz", "name that tune"): `update_settings({"overlays": {"trivia":
  {"enabled": true}}})`. Each song plays sound-only behind a cover for `audio_seconds` (20), then the
  video without its title for `video_seconds` (20), then the answer appears. Cover `bg.type` "blur"
  (blurred copy of the video) is an easier variant. Check with `preview_image("song", seconds=5/30/50)`.
- Songs can be referenced by position number, id, or part of the title.
- `update_settings` deep-merges, so send only what changes.
- Short videos (< song length) play in full by default; `settings.short_mode = "loop"` loops them.
- Local files: `add_local_videos("C:\\path\\to\\folder")`.
- Tell the user where the result is (`render_status` → `output`) and offer `open_app()`.
