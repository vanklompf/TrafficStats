# Remaining Issues

## Medium Priority

1. **Shadowed `range` builtin in `main.py`** — The `api_stats` parameter `range` shadows the Python builtin. Rename to `range_key` or `time_range`.

2. **`_enforce_cache_limit` issues in `intrusions.py`** — Calls `.stat()` twice per file; `all_files.pop(0)` is O(n); `FileNotFoundError` not caught if a file disappears between `stat()` and `unlink()`; `VIDEO_CACHE_MAX_BYTES` is a `float` instead of `int`.

3. **No SRI on CDN scripts** — Three scripts loaded from jsDelivr in `index.html` have no `integrity` or `crossorigin` attributes. Pin to exact versions and add SRI hashes.

4. **No ARIA attributes** — The tab widget, modal dialog, and chart canvas lack proper accessibility semantics (roles, labels, focus management).

5. **Hardcoded NAS path in docker-compose** — `/mnt/nas/media/backup/kamery/kamera_front` is machine-specific. Parameterize via an env var.

6. **CI workflow issues (`docker-publish.yml`)** — No test step before push; ~~`build-push-action` at v5 (latest is v6)~~.

7. **No path-containment check for media files** — `_validate_filename` regex prevents most traversal, but no `Path.resolve()` check confirms the final path is under `MEDIA_PATH`. Add a containment check.

## Low Priority

8. **60-second timer runs on inactive tab** — Traffic data refresh runs even on the Intrusions tab. Gate behind a visibility check.

9. **UTC date default in date picker** — `new Date().toISOString().slice(0, 10)` can return tomorrow for users west of UTC. Use local date instead.

10. **Render-blocking CDN scripts** — Chart.js scripts in `<head>` lack `defer` or `async`.

11. **No `prefers-reduced-motion` support** — CSS transitions don't respect the user's motion preference.

12. **README gaps** — Missing `TZ` in config table, no GHCR pull instructions, no architecture overview, no license file.

13. **No focus-visible styles** — Interactive elements lack `:focus-visible` styles.

14. **Video autoplay without fallback** — `video.autoplay = true` silently fails if the video has audio. Handle the autoplay rejection or set `muted`.

15. **`docker-compose.test.yml` uses prod `.env`** — Should use a separate `.env.test`. Also `restart: unless-stopped` is odd for a test container.
