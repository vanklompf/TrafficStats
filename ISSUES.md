# Remaining Issues

## High Priority

1. ~~**Synchronous `requests` in async FastAPI**~~ — Resolved: replaced `requests` with `httpx` in the Dahua listener.

## Medium Priority

2. **Shadowed `range` builtin in `main.py`** — The `api_stats` parameter `range` shadows the Python builtin. Rename to `range_key` or `time_range`.

3. ~~**Race condition in video conversion**~~ — Resolved: added per-file lock in `convert_dav_to_mp4` so concurrent requests wait for the first conversion.

4. **`_enforce_cache_limit` issues in `intrusions.py`** — Calls `.stat()` twice per file; `all_files.pop(0)` is O(n); `FileNotFoundError` not caught if a file disappears between `stat()` and `unlink()`; `VIDEO_CACHE_MAX_BYTES` is a `float` instead of `int`.

5. **No SRI on CDN scripts** — Three scripts loaded from jsDelivr in `index.html` have no `integrity` or `crossorigin` attributes. Pin to exact versions and add SRI hashes.

6. **No ARIA attributes** — The tab widget, modal dialog, and chart canvas lack proper accessibility semantics (roles, labels, focus management).

7. **Hardcoded NAS path in docker-compose** — `/mnt/nas/media/backup/kamery/kamera_front` is machine-specific. Parameterize via an env var.

8. **CI workflow issues (`docker-publish.yml`)** — Tag trigger `'*'` is too broad (tighten to `'v*'`); no test step before push; no build cache; `build-push-action` at v5 (latest is v6).

10. ~~**Hardcoded default credentials in `DahuaListener`**~~ — Resolved: removed default credentials; `user` and `password` are now required, and `create_listener_from_env` refuses to start if `DAHUA_USER`/`DAHUA_PASS` are unset.

11. **No path-containment check for media files** — `_validate_filename` regex prevents most traversal, but no `Path.resolve()` check confirms the final path is under `MEDIA_PATH`. Add a containment check.

## Low Priority

12. **60-second timer runs on inactive tab** — Traffic data refresh runs even on the Intrusions tab. Gate behind a visibility check.

13. **UTC date default in date picker** — `new Date().toISOString().slice(0, 10)` can return tomorrow for users west of UTC. Use local date instead.

14. **Render-blocking CDN scripts** — Chart.js scripts in `<head>` lack `defer` or `async`.

15. **`canvas { width: 100% !important; }` fights Chart.js** — Set width on the parent container instead.

16. **No `prefers-reduced-motion` support** — CSS transitions don't respect the user's motion preference.

17. **Logging configuration at import time** — `logging.basicConfig()` runs at module import; move to the lifespan handler.

18. ~~**Environment variable parsing can crash**~~ — Resolved: added `_parse_int_env` / `_parse_float_env` helpers that log a warning and fall back to the default on invalid input.

19. **`.gitignore`: `data-test/*` should be `data-test/`** — Consistent with `data/` pattern.

20. **README gaps** — Missing `TZ` in config table, no GHCR pull instructions, no architecture overview, no license file.

21. **No focus-visible styles** — Interactive elements lack `:focus-visible` styles.

22. **Video autoplay without fallback** — `video.autoplay = true` silently fails if the video has audio. Handle the autoplay rejection or set `muted`.

23. **`docker-compose.test.yml` uses prod `.env`** — Should use a separate `.env.test`. Also `restart: unless-stopped` is odd for a test container.
