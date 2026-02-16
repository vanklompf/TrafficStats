# Remaining Issues

## High Priority

1. **Container runs as root** — The Dockerfile doesn't create a non-root user. Add `RUN adduser --disabled-password appuser` and `USER appuser`.

2. **Synchronous `requests` in async FastAPI** — The Dahua listener uses `requests` (synchronous HTTP client). While it runs in a background thread, consider switching to `httpx` for consistency.

## Medium Priority

3. **Shadowed `range` builtin in `main.py`** — The `api_stats` parameter `range` shadows the Python builtin. Rename to `range_key` or `time_range`.

4. **Race condition in video conversion** — Two concurrent requests for the same video can both start ffmpeg. Add a per-file lock or "in progress" sentinel.

5. **`_enforce_cache_limit` issues in `intrusions.py`** — Calls `.stat()` twice per file; `all_files.pop(0)` is O(n); `FileNotFoundError` not caught if a file disappears between `stat()` and `unlink()`; `VIDEO_CACHE_MAX_BYTES` is a `float` instead of `int`.

6. **No SRI on CDN scripts** — Three scripts loaded from jsDelivr in `index.html` have no `integrity` or `crossorigin` attributes. Pin to exact versions and add SRI hashes.

7. **No ARIA attributes** — The tab widget, modal dialog, and chart canvas lack proper accessibility semantics (roles, labels, focus management).

8. **Hardcoded NAS path in docker-compose** — `/mnt/nas/media/backup/kamery/kamera_front` is machine-specific. Parameterize via an env var.

9. **CI workflow issues (`docker-publish.yml`)** — Tag trigger `'*'` is too broad (tighten to `'v*'`); no test step before push; no build cache; `build-push-action` at v5 (latest is v6).

10. **XSS risk in `events.html`** — (File removed, but if re-added: use `textContent` / DOM APIs instead of `innerHTML` with template literals.)

11. **Hardcoded default credentials in `DahuaListener`** — Constructor defaults to `user="admin"`, `password="admin"`. Consider requiring these with no default.

12. **No path-containment check for media files** — `_validate_filename` regex prevents most traversal, but no `Path.resolve()` check confirms the final path is under `MEDIA_PATH`. Add a containment check.

## Low Priority

13. **60-second timer runs on inactive tab** — Traffic data refresh runs even on the Intrusions tab. Gate behind a visibility check.

14. **UTC date default in date picker** — `new Date().toISOString().slice(0, 10)` can return tomorrow for users west of UTC. Use local date instead.

15. **Render-blocking CDN scripts** — Chart.js scripts in `<head>` lack `defer` or `async`.

16. **`canvas { width: 100% !important; }` fights Chart.js** — Set width on the parent container instead.

17. **No `prefers-reduced-motion` support** — CSS transitions don't respect the user's motion preference.

18. **Logging configuration at import time** — `logging.basicConfig()` runs at module import; move to the lifespan handler.

19. **Environment variable parsing can crash** — `DAHUA_PORT` uses `int()` and `VIDEO_CACHE_MAX_GB` uses `float()` without try/except.

20. **`.gitignore`: `data-test/*` should be `data-test/`** — Consistent with `data/` pattern.

21. **README gaps** — Missing `TZ` in config table, no GHCR pull instructions, no architecture overview, no license file.

22. **No focus-visible styles** — Interactive elements lack `:focus-visible` styles.

23. **Video autoplay without fallback** — `video.autoplay = true` silently fails if the video has audio. Handle the autoplay rejection or set `muted`.

24. **`docker-compose.test.yml` uses prod `.env`** — Should use a separate `.env.test`. Also `restart: unless-stopped` is odd for a test container.
