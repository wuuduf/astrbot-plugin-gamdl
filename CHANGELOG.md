# Changelog

All notable changes to this project will be documented in this file.

## [0.1.0] - 2026-04-06

### Added
- Initial release of `astrbot_plugin_gamdl` as a standalone AstrBot plugin (no external service process).
- Built-in local backend (`core/backend.py`) with async queue workers for long-running download jobs.
- Apple Music capabilities powered by `gamdl`:
  - search (`song` / `album` / `artist`)
  - URL resolve
  - artist children browse (`albums` / `music-videos`)
  - download jobs with `one` / `zip` transfer mode
  - artwork export
  - animated artwork export (ffmpeg required)
  - lyrics export (`lrc` / `ttml`, song and album)
- NapCat/OneBot sending strategy:
  - file/image/video support
  - video-send failure fallback to file
  - human-readable permission/path errors
- Session-scoped persistent settings keyed by `unified_msg_origin`.

### Changed
- Defaults for auto-attachment flags are now disabled:
  - `include_lyrics = false`
  - `include_cover = false`
  - `include_animated_cover = false`
- Plugin initialization health check now verifies local backend readiness instead of remote service connectivity.

### Notes
- `station` URL download is currently unsupported due upstream `gamdl` limitations.
- `flac` selection falls back to `alac` in this plugin implementation.
