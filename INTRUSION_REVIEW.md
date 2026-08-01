# Intrusion Detection Review

Review scope: intrusion event ingestion, event-to-media matching, snapshot and
video handling, the image pipeline leading to LLM analysis, background job
architecture, duplication, and test coverage.

## Architecture

The current processing flow is:

```text
Dahua event stream
  -> application receipt timestamp
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

`match_media_for_events()` sorts recordings by start time and selects the first
interval matching a plus-or-minus 30-second tolerance. An earlier recording
matching only through tolerance can beat a later recording that actually
contains the event.

**Status:** Addressed.

DAV discovery and selection are now shared by event enrichment and debounce.
The deterministic ranking prefers exact containment, then nearest boundary,
nearest recording start, shorter duration, and stable filename/date
tie-breakers.

References:

- `app/intrusions.py:280-307`
- `app/intrusions.py:198-233`

The debounce lookup is a separate implementation and iterates unsorted
filesystem results. Event suppression can therefore use a different recording
from the one displayed and analyzed.

### 3. Event-to-media identity is weak

**Severity:** High

Camera event sequence/index information is discarded and the database uses the
application receipt time. Media filename metadata such as type and channel is
accepted by broad regular expressions but ignored during matching.

References:

- `app/dahua.py:147-168`
- `app/dahua.py:185-197`
- `app/database.py:99-115`
- `app/intrusions.py:137-144`
- `app/intrusions.py:270-307`

Network delays, reconnect buffering, multiple channels, or multiple camera
event types can associate an intrusion with unrelated media.

Recommended change: persist the camera event identifier, source timestamp,
channel, and IVS rule where available. Parse equivalent filename metadata and
use it before timestamp proximity.

### 4. Analysis payload and resource use are unbounded

**Severity:** High

All candidate frames are written before filtering. Every motion-significant
frame is retained, encoded in memory, and sent in one Ollama request. There is
no recording-duration, candidate-frame, selected-frame, pixel, byte, or model
context budget.

References:

- `app/analysis.py:162-223`
- `app/analysis.py:247-267`
- `app/analysis.py:493-523`

Camera shake, rain, or lighting changes can create a very large request, exhaust
temporary storage or memory, and block the only worker.

Recommended change: impose a maximum recording duration, maximum selected
frames, and maximum encoded payload. Preserve event-time and uniformly spaced
fallback frames when reducing the set.

### 5. An exception can terminate the only analysis worker

**Severity:** High

The worker loop has cleanup in `finally` but no outer exception handler around
`_process_one()`. Database access and media matching occur outside the inner
processing handler. An exception from those operations, or while recording a
failure, can terminate the daemon thread.

References:

- `app/analysis.py:410-423`
- `app/analysis.py:425-463`
- `app/analysis.py:550-552`
- `app/main.py:143-150`

The health endpoint reports camera-listener state but not analysis-worker
liveness.

Recommended change: supervise each job with an outer exception boundary,
record failure where possible, continue the loop, and expose worker liveness and
current job in health data.

### 6. Intrusion media and mutation endpoints are unauthenticated

**Severity:** High

Live camera streams, snapshots, original recordings, converted videos, LLM
results, queue information, and retry actions have no authentication or
authorization dependency. Event responses also expose container filesystem
paths.

References:

- `app/main.py:164-230`
- `app/main.py:253-332`
- `app/main.py:432-496`
- `app/main.py:367`
- `app/main.py:417`

Recommended change: add authentication to all camera, intrusion, media, and
analysis routes. Remove `video_path` from the external response.

### 7. Post-upload intrusion deduplication is not executed

**Severity:** High

Initial debounce falls back to a fixed time window while recordings are absent.
The reconciliation function intended to remove events covered by the same
eventual recording has no callers.

References:

- `app/database.py:117-156`
- `app/database.py:172-219`

Long recordings can produce duplicate events, notifications, and analyses for
one incident.

Recommended change: reconcile after stable media discovery and associate all
camera triggers covered by one recording with one incident. Do not simply
delete rows after analysis without also handling analysis foreign keys.

### 8. Media associations are mutable but analyses are permanent

**Severity:** Medium

The selected snapshot/video and source fingerprint are not stored with the
event analysis. Matching is recalculated from the current filesystem for every
request and worker run. A later upload can change the displayed recording while
the stored analysis continues to describe an earlier match.

References:

- `app/database.py:69-77`
- `app/main.py:342-368`
- `app/main.py:392-418`
- `app/analysis.py:441-448`

Recommended change: persist the selected media identity, timestamp range, and
source fingerprint when scheduling analysis. Invalidate or rerun analysis when
that association changes.

### 9. Queue policy causes head-of-line blocking and restart storms

**Severity:** Medium

A missing recording can occupy the single worker for the entire media wait.
All failure classes are stored as `failed`, and historical failures are queued
oldest-first on restart by default.

References:

- `app/analysis.py:383-451`
- `app/database.py:501-520`

Permanent failures can repeatedly delay current security incidents.

Recommended change: distinguish `media_pending`, retryable failure, and terminal
failure. Use delayed retries and prioritize current incidents over backfill.

### 10. Analysis retry has a race

**Severity:** Medium

The retry endpoint deletes the existing result before enqueueing. Enqueue
silently ignores an event that is still marked as processing. A retry arriving
after result storage but before processing-set cleanup can erase the result
without scheduling another run.

References:

- `app/main.py:313-327`
- `app/analysis.py:341-355`

Recommended change: make retry an atomic worker operation that either marks a
running job for rerun or enqueues it before replacing visible state.

### 11. Empty model output is stored as success

**Severity:** Medium

An empty Ollama message becomes `None`, but status is still stored as `done`.
Completion state and response schema are not validated.

Reference: `app/analysis.py:538-544`.

Recommended change: require non-empty content and a valid completed response;
otherwise record a classified provider failure.

### 12. Frame selection can omit the actual intrusion

**Severity:** Medium

The first frame is always retained and later frames are selected using global
mean grayscale difference against the last retained frame. Small, slow, or
stationary subjects can remain below threshold, while broad lighting changes
can dominate selection.

References:

- `app/analysis.py:180-210`
- `app/analysis.py:233-244`

Recommended change: retain frames near the event time, include fixed temporal
coverage, cap motion-selected frames, and provide per-frame timing to the model.

### 13. DST transitions can corrupt media ranges

**Severity:** Medium

Naive camera-local timestamps are assigned a timezone without resolving
ambiguous DST folds. A recording ending at an earlier wall-clock time is
assumed to cross midnight. During fall-back this can turn a short recording
into an approximately 24-hour interval.

References:

- `app/intrusions.py:130-135`
- `app/intrusions.py:170-187`

Recommended change: explicitly resolve ambiguous local times using neighboring
timestamps and constrain inferred recording durations to a sane maximum.

## Duplication And Architectural Smells

### Competing media matchers

`get_recording_end_utc()` and `match_media_for_events()` independently scan and
match DAV files with different ordering. Debounce, API presentation, and LLM
analysis can disagree about the recording associated with an event.

### Three DAV conversion implementations

Conversion exists separately in:

- `app/intrusions.py` for browser playback
- `app/analysis.py` for LLM frame extraction
- `tools/test_video_analysis.py` for experimentation

They differ in scaling, hardware acceleration, audio, timeout, and caching
behavior. Production may transcode one recording twice while tests exercise a
third path.

### Production and harness image pipelines have diverged

Motion difference, extraction, encoding, prompt construction, and Ollama calls
are duplicated between `app/analysis.py` and `tools/test_video_analysis.py`.
For example, mask-size mismatch fails open in production but raises in the
harness, so benchmark behavior does not exactly represent deployment behavior.

### Persistence owns media and intrusion policy

`app/database.py` imports filesystem matching logic while deciding whether an
event may be inserted. Persistence therefore depends on media infrastructure,
and camera ingestion can synchronously scan a NAS mount.

### AnalysisWorker has too many responsibilities

`AnalysisWorker` owns queueing, thread lifecycle, database operations, media
polling, conversion, image processing, provider transport, response parsing,
and retry policy. Concrete module imports and import-time configuration make
these parts difficult to test independently.

### intrusions.py combines unrelated concerns

The module combines timezone handling, filename parsing, media discovery,
matching, hardware probing, conversion, video caching, and thumbnail caching.
It also maintains a process-global per-file lock registry whose entries are
never removed.

### API event enrichment is duplicated

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
`tools/test_video_analysis.py` is a benchmark harness rather than a production
integration test. Commit `dff7629` introduced focused tests for stable/growing
files and cache fingerprint invalidation.

Additional priority coverage:

- Exact recording containment must beat tolerance-only matches.
- Filesystem enumeration order must not affect matching.
- Multiple events and channels competing for the same media.
- Camera event delay, replay, and retained source identity.
- Local/UTC date boundaries and DST gaps/folds.
- Corrupt DAVs and interrupted ffmpeg conversion.
- Frame-count, duration, pixel, and payload limits.
- Empty, malformed, timeout, 429, and 5xx Ollama responses.
- Worker survival after database, filesystem, and provider exceptions.
- Retry while queued, processing, and immediately after completion.
- Shutdown during media wait, ffmpeg, and provider requests.
- Authentication and authorization for all media and mutation endpoints.
