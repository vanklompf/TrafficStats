# Intrusion Detection Review

Review scope: intrusion event ingestion, event-to-media matching, snapshot and
video handling, the image pipeline leading to LLM analysis, background job
architecture, duplication, and test coverage.

Runtime inspection: 2026-08-02, against the running `trafficstats:local`
container and its persisted SQLite database. The inspection was read-only and
did not trigger analysis retries or media conversions.

## Current Priority

| Priority | Finding | Current assessment |
| --- | --- | --- |
| P0 | Runtime blocker: Ollama is unreachable | The API reports healthy while every current analysis fails because the deployed container cannot resolve `ollama`. |
| P1 | 6. Unauthenticated APIs | High: the application is reachable on the LAN/Tailnet through Traefik and intrusion/queue endpoints returned HTTP 200 without credentials. |
| P1 | 5. Worker supervision and health | High: one uncaught job exception can kill the only worker, and health exposes neither worker liveness nor provider readiness. |
| P2 | 3, 4, 7, 8, 10, 11, 12 | Open or partial correctness and resource-control work; see individual findings. |
| P3 | 13 and architectural cleanup | Lower immediate impact than restoring and supervising analysis, but still valid. |
| Closed | 1, 2, and 9 | Stable uploads, deterministic recording selection, and classified delayed analysis retries are addressed and covered by focused tests. |

## Runtime Snapshot

- The container was healthy at the Docker level with a 4 GiB memory limit, and
  the camera listener was connected. `/api/health` returned
  `{"status":"ok","camera_listener":"running"}`.
- The container was attached only to Docker's default `bridge` network, while
  the `ollama` container was attached to the external `ollama` network. DNS
  lookup for `ollama` failed from the application container even though
  `OLLAMA_HOST` was `http://ollama:11434`.
- Startup backfilled 16 events from the configured seven-day window. Four had
  matching videos; frame extraction produced 5-50 selected frames, 139-1,441
  KiB of JPEG data, and 9.0-41.5 second spans. All four then failed provider
  lookup. The other 12 immediately became `failed` because no video matched.
- After that pass the queue endpoint reported zero pending jobs, although all
  16 jobs had failed. A restart will select those failed rows again.
- Persisted totals were 1,370 intrusion events: 1,023 `done`, 146 `failed`, and
  201 without an analysis row. Two historical `done` rows had empty analysis.
- All 16 recent events had null source timestamp, event ID, sequence, and
  channel because they were recorded before the identity-aware build started.
  Live behavior of the new identity fields therefore remains unproven.
- The hostname-routed endpoint and direct host port both served intrusion data
  without authentication. The hostname resolved to a Tailscale address during
  inspection, so public Internet exposure was not established. Event responses
  include analysis text and container media paths; media and retry routes use
  the same unauthenticated application.

## Architecture

The current processing flow is:

```text
Dahua event stream
  -> normalized source identity and timestamp when available
  -> application receipt timestamp fallback
  -> SQLite event
  -> ntfy notification
  -> in-memory analysis queue
  -> filesystem media scan and timestamp match
  -> DAV-to-MP4 conversion
  -> sampled JPEG extraction
  -> motion filtering
  -> base64 image payload
  -> Ollama vision model
  -> SQLite analysis result
```

Snapshots are used for gallery display. LLM analysis uses frames extracted from
the matched DAV recording rather than the camera snapshot.

### Runtime blocker: the provider is disconnected but health is green

**Severity:** Critical operational blocker

The deployed application is not attached to the Docker network that provides
the `ollama` DNS name. Every video-backed job observed during startup performed
conversion and frame extraction, then failed with `httpx.ConnectError: [Errno
-2] Name or service not known`. `/api/health` nevertheless returned `status:
ok`, and the empty queue after processing did not distinguish success from a
drained batch of failures.

Immediate action: redeploy the container on the external `ollama` network and
verify provider DNS and `/api/tags` from the application container. Then add
provider readiness and worker liveness to health, and use a provider-level
circuit breaker so a known outage does not repeatedly pay conversion cost.

Primary modules:

| Module | Responsibility |
| --- | --- |
| `app/dahua.py` | Camera event subscription, parsing, and classification |
| `app/database.py` | Event persistence, intrusion debounce, and analysis results |
| `app/intrusions.py` | Time conversion, media matching, conversion, and caches |
| `app/analysis.py` | Analysis queue, frame extraction, Ollama calls, and result storage |
| `app/main.py` | Application lifecycle, APIs, and media serving |
| `app/notifications.py` | Immediate ntfy notification delivery |

## Findings

### 1. Growing DAV files can be processed before upload completes

**Original severity:** Critical

**Current severity:** Low residual risk

The Dahua camera writes directly to the final DAV filename. File existence did
not indicate upload completion, allowing ffmpeg to process an incomplete file.
This could produce a failed analysis or a valid but truncated cached MP4 and
analysis.

**Status:** Addressed by commit `dff7629`.

The application now requires size and nanosecond mtime to remain unchanged for
10 seconds, checks the source fingerprint during analysis and conversion, and
associates cached MP4 files with the source fingerprint. A changed source
invalidates the cache and prevents an analysis from being published as done.

Residual risk: an upload that pauses for more than 10 seconds and later resumes
can temporarily appear stable. The post-conversion and pre-publication checks
reduce this risk but cannot eliminate a future resume without upload-side
atomicity.

### 2. Overlapping recordings can select the wrong video

**Original severity:** High

**Current severity:** Closed

Original issue:

`match_media_for_events()` sorts recordings by start time and selects the first
interval matching a plus-or-minus 30-second tolerance. An earlier recording
matching only through tolerance can beat a later recording that actually
contains the event.

**Status:** Addressed.

DAV discovery and selection are now shared by event enrichment and debounce.
The deterministic ranking prefers exact containment, then nearest boundary,
nearest recording start, shorter duration, and stable filename/date
tie-breakers.

Current references:

- `app/intrusions.py:292-394`
- `app/intrusions.py:452-454`
- `app/database.py:166-180`
- `tests/test_intrusions.py:130-165`

### 3. Event-to-media identity is weak

**Original severity:** High

**Current severity:** Medium

Original issue:

Camera event sequence/index information is discarded and the database uses the
application receipt time. Media filename metadata such as type and channel is
accepted by broad regular expressions but ignored during matching.

Network delays, reconnect buffering, multiple channels, or multiple camera
event types can associate an intrusion with unrelated media.

Recommended change: persist the camera event identifier, source timestamp,
channel, and IVS rule where available. Parse equivalent filename metadata and
use it before timestamp proximity.

**Status:** Partially addressed.

Camera event ID, sequence, normalized source timestamp, channel, and IVS rule
are now retained in SQLite. DAV and snapshot metadata is parsed into type,
channel, stream, and index fields. Matching uses camera source time instead of
receipt time when available, rejects known channel conflicts, and prefers an
exact channel match before applying timestamp ranking. Legacy events and media
filenames without identity metadata continue to use receipt-time proximity.
However, parsed media type, stream, and index are not used for ranking;
persisted event ID and sequence are not correlated with media index; and the
chosen association is not persisted. The live database has not yet received an
identity-populated event, so this path is covered by tests but not runtime
evidence.

Current references:

- `app/dahua.py:204-258`
- `app/database.py:100-198`
- `app/intrusions.py:340-452`
- `tests/test_event_identity.py:14-169`

### 4. Analysis payload and resource use are unbounded

**Severity:** Medium

**Status:** Open.

All candidate frames are written before filtering. Every motion-significant
frame is retained, encoded in memory, and sent in one Ollama request. There is
no recording-duration, candidate-frame, selected-frame, or encoded-byte budget.
The configured 512-pixel target width, Ollama `num_ctx`, ffmpeg timeouts, and 4
GiB container memory limit constrain parts of the failure, but do not bound the
number or aggregate size of images.

References:

- `app/analysis.py:169-274`
- `app/analysis.py:505-556`

Camera shake, rain, or lighting changes can create a very large request, exhaust
temporary storage or memory, and block the only worker.

Observed jobs were modest rather than pathological: at most 50 selected frames
and 1,441 KiB encoded image data from a 41.5-second span. This lowers immediate
priority relative to the provider outage and authentication, but is not a
safety bound.

Recommended change: impose a maximum recording duration, maximum selected
frames, and maximum encoded payload. Preserve event-time and uniformly spaced
fallback frames when reducing the set.

### 5. An exception can terminate the only analysis worker

**Severity:** High

**Status:** Open and elevated by runtime observability.

The worker loop has cleanup in `finally` but no outer exception handler around
`_process_one()`. Database access and media matching occur outside the inner
processing handler. An exception from those operations, or while recording a
failure, can terminate the daemon thread.

References:

- `app/analysis.py:417-484`
- `app/analysis.py:599-604`
- `app/main.py:143-150`

The health endpoint reports camera-listener state but not analysis-worker
liveness.

The handled provider exceptions observed at runtime did not kill the worker,
but health remained green throughout total analysis failure. An exception from
event lookup, media matching, stable-file polling, or failure persistence can
still escape the job and terminate the thread.

Recommended change: supervise each job with an outer exception boundary,
record failure where possible, continue the loop, and expose worker liveness and
current job in health data.

### 6. Intrusion media and mutation endpoints are unauthenticated

**Severity:** High

**Status:** Open and externally reachable.

Live camera streams, snapshots, original recordings, converted videos, LLM
results, queue information, and retry actions have no authentication or
authorization dependency. Event responses also expose container filesystem
paths.

Runtime inspection confirmed Traefik labels for a TLS hostname and received
HTTP 200 without credentials from both hostname-routed intrusion-date and
analysis-queue endpoints. The hostname resolved to a Tailscale address, so this
does not prove public Internet exposure, but access is not limited to localhost:
Compose also publishes `3896` on `0.0.0.0` for the LAN.

References:

- `app/main.py:164-230`
- `app/main.py:253-332`
- `app/main.py:432-496`
- `app/main.py:367`
- `app/main.py:417`

Recommended change: add authentication to all camera, intrusion, media, and
analysis routes. Remove `video_path` from the external response.

### 7. Post-upload intrusion deduplication is not executed

**Severity:** Medium

**Status:** Open, but not demonstrated in the current sample.

Initial debounce falls back to a fixed time window while recordings are absent.
The reconciliation function intended to remove events covered by the same
eventual recording has no callers.

References:

- `app/database.py:128-255`

Long recordings can produce duplicate events, notifications, and analyses for
one incident.

The 16 recent events contained four gaps under five minutes but none under the
120-second fallback debounce. Four events matched four distinct recordings, so
the current day does not prove post-upload duplication. The reconciliation
function still has no caller, ignores the newer event identity fields, and can
delete events without SQLite foreign-key enforcement (`PRAGMA foreign_keys`
was 0 in the live connection).

Recommended change: reconcile after stable media discovery and associate all
camera triggers covered by one recording with one incident. Do not simply
delete rows after analysis without also handling analysis foreign keys.

### 8. Media associations are mutable but analyses are permanent

**Severity:** Medium

**Status:** Open.

The selected snapshot/video and source fingerprint are not stored with the
event analysis. Matching is recalculated from the current filesystem for every
request and worker run. A later upload can change the displayed recording while
the stored analysis continues to describe an earlier match.

References:

- `app/database.py:42-84`
- `app/main.py:342-368`
- `app/main.py:392-418`
- `app/analysis.py:439-466`

Recommended change: persist the selected media identity, timestamp range, and
source fingerprint when scheduling analysis. Invalidate or rerun analysis when
that association changes.

### 9. Queue policy causes head-of-line blocking and restart storms

**Original severity:** High during dependency outages

**Current severity:** Low residual operational risk

**Status:** Addressed.

A missing recording can occupy the single worker for the entire media wait.
All failure classes are stored as `failed`, and historical failures are queued
oldest-first on restart by default.

References:

- `app/analysis.py:282-299`
- `app/analysis.py:390-480`
- `app/database.py:539-557`

Permanent failures can repeatedly delay current security incidents.

Analysis state now distinguishes `media_pending`, `retryable_failure`, and
`terminal_failure`, with persisted attempt counts, failure reasons, and retry
times. Provider transport failures use capped exponential backoff. Missing
media is checked once per attempt and rescheduled while the upload window is
open instead of holding the only worker in a polling loop.

The in-memory queue prioritizes live incidents over due retries and newest-first
startup backfill. Restart recovery includes only unprocessed, interrupted, and
retryable jobs; legacy undifferentiated `failed` rows migrate to terminal
failure and are not replayed. Delayed jobs remain visible through the queue API,
including their source and scheduled time, and the dashboard continues polling
scheduled states.

Current references:

- `app/analysis.py:328-694`
- `app/database.py:554-633`
- `app/main.py:259-310`
- `tests/test_analysis_queue.py:15-164`

Residual risk: retries still repeat conversion and frame extraction when they
become due if the provider remains unavailable. Provider readiness and a
circuit breaker are tracked separately with worker supervision and health.

### 10. Analysis retry has a race

**Severity:** Medium

**Status:** Open.

The retry endpoint deletes the existing result before enqueueing. Enqueue
silently ignores an event that is still marked as processing. A retry arriving
after result storage but before processing-set cleanup can erase the result
without scheduling another run.

References:

- `app/main.py:313-327`
- `app/analysis.py:348-430`

Recommended change: make retry an atomic worker operation that either marks a
running job for rerun or enqueues it before replacing visible state.

### 11. Empty model output is stored as success

**Severity:** Medium

**Status:** Open and present in persisted data.

An empty Ollama message becomes `None`, but status is still stored as `done`.
Completion state and response schema are not validated.

The live database contained two `done` rows whose analysis was null or empty.
The inspection did not attribute their origin, but the current response path
still permits exactly this state.

Reference: `app/analysis.py:577-593`.

Recommended change: require non-empty content and a valid completed response;
otherwise record a classified provider failure.

### 12. Frame selection can omit the actual intrusion

**Severity:** Medium

**Status:** Open.

The first frame is always retained and later frames are selected using global
mean grayscale difference against the last retained frame. Small, slow, or
stationary subjects can remain below threshold, while broad lighting changes
can dominate selection.

References:

- `app/analysis.py:110-251`

Recommended change: retain frames near the event time, include fixed temporal
coverage, cap motion-selected frames, and provide per-frame timing to the model.

### 13. DST transitions can corrupt media ranges

**Severity:** Low

**Status:** Open.

Naive camera-local timestamps are assigned a timezone without resolving
ambiguous DST folds. A recording ending at an earlier wall-clock time is
assumed to cross midnight. During fall-back this can turn a short recording
into an approximately 24-hour interval.

References:

- `app/intrusions.py:137-142`
- `app/intrusions.py:261-333`

Recommended change: explicitly resolve ambiguous local times using neighboring
timestamps and constrain inferred recording durations to a sane maximum.

The deployment uses `Europe/Warsaw`, so folds and gaps are relevant, but the
observed recordings were under one minute and provided no runtime evidence of
this failure. Keep a duration sanity limit even if fold inference is deferred.

## Duplication And Architectural Smells

### Competing media matchers

**Status:** Addressed.

`get_recording_end_utc()` and `match_media_for_events()` now share media
scanning and deterministic selection. Focused tests cover exact containment,
tolerance ranking, overlap, and filesystem enumeration order.

### Three DAV conversion implementations

**Status:** Open.

Conversion exists separately in:

- `app/intrusions.py` for browser playback
- `app/analysis.py` for LLM frame extraction
- `tools/test_video_analysis.py` for experimentation

They differ in scaling, hardware acceleration, audio, timeout, and caching
behavior. Production may transcode one recording twice while tests exercise a
third path.

### Production and harness image pipelines have diverged

**Status:** Open.

Motion difference, extraction, encoding, prompt construction, and Ollama calls
are duplicated between `app/analysis.py` and `tools/test_video_analysis.py`.
For example, mask-size mismatch fails open in production but raises in the
harness, so benchmark behavior does not exactly represent deployment behavior.

### Persistence owns media and intrusion policy

**Status:** Open.

`app/database.py` imports filesystem matching logic while deciding whether an
event may be inserted. Persistence therefore depends on media infrastructure,
and camera ingestion can synchronously scan a NAS mount.

### AnalysisWorker has too many responsibilities

**Status:** Open.

`AnalysisWorker` owns queueing, thread lifecycle, database operations, media
polling, conversion, image processing, provider transport, response parsing,
and retry policy. Concrete module imports and import-time configuration make
these parts difficult to test independently.

### intrusions.py combines unrelated concerns

**Status:** Open.

The module combines timezone handling, filename parsing, media discovery,
matching, hardware probing, conversion, video caching, and thumbnail caching.
It also maintains a process-global per-file lock registry whose entries are
never removed.

### API event enrichment is duplicated

**Status:** Open.

The list and detail endpoints independently attach analysis fields, media URLs,
cache state, and filesystem paths. The response shapes can drift.

## Recommended Target Boundaries

### Incident registration

Accept a normalized camera event containing camera identity, source event ID,
source timestamp, channel, IVS rule, and receipt timestamp. Apply intrusion
policy outside the camera transport and persistence modules.

### Media catalog and association

Index stable uploads once. Parse filename metadata into structured records,
rank associations deterministically, and persist the chosen media identity and
fingerprint. Debounce, UI, and analysis should use the same association.

### Analysis job

Consume a persisted media association. Bound frame and payload resources, track
attempts and classified failures, and separate orchestration from image
selection and provider transport.

These boundaries do not require separate services. They can initially be small
modules and database tables within the existing application process.

## Test Gaps

The repository previously had no assertion-based application test suite.
`tools/test_video_analysis.py` remains a benchmark harness rather than a
production integration test. Current focused tests cover stable/growing files,
cache fingerprint invalidation, deterministic shared recording selection,
source-time normalization, identity persistence, and channel-aware matching.

Additional priority coverage:

- Multiple events and channels competing for the same media.
- Camera event delay, replay, sequence/media-index correlation, and live
  identity-populated ingestion.
- DST gaps/folds and duration sanity limits.
- Corrupt DAVs and interrupted ffmpeg conversion.
- Frame-count, duration, pixel, and payload limits.
- Empty, malformed, timeout, 429, and 5xx Ollama responses.
- Provider DNS/readiness failure and circuit-breaker behavior.
- Worker survival after database, filesystem, and provider exceptions.
- Retry while queued, processing, and immediately after completion.
- Shutdown during media wait, ffmpeg, and provider requests.
- Authentication and authorization for all media and mutation endpoints.
