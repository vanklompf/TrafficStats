# TrafficStats

Web application that counts vehicles using a Dahua camera's built-in IVS (tripwire/line-crossing detection). Events are stored in SQLite and displayed as a line chart with 5-minute resolution.

## Quick start

```bash
cp .env.example .env   # edit with your camera IP and credentials
docker compose up -d --build
```

Dashboard: **http://localhost:3896**

## Configuration

Edit `.env`:

| Variable | Description | Default |
|---|---|---|
| `DAHUA_HOST` | Camera IP address | `192.168.1.108` |
| `DAHUA_PORT` | Camera HTTP port | `80` |
| `DAHUA_USER` | Camera username | `admin` |
| `DAHUA_PASS` | Camera password | `admin` |
| `DAHUA_EVENTS` | Event codes to listen for | `All` |
| `DAHUA_IVS_NAMES` | Comma-separated IVS rule names to accept for traffic counting (empty = accept all) | `CarDetection` |
| `DAHUA_INTRUSION_IVS_NAME` | IVS rule name for intrusion detection | `intrusion` |
| `CITY` | City name for location (e.g. `Helsinki`, `London, UK`). Lat/lon and timezone are looked up automatically. | — |
| `INTRUSION_MEDIA_PATH` | Container path where camera FTP uploads are mounted | `/media/kamera_front` |
| `VIDEO_CACHE_MAX_GB` | Max disk space for converted video cache | `20` |

When **CITY** is set, traffic events are only recorded between sunrise and sunset at that location. The chart shows shaded bands for periods when no collection is done (night). Intrusion events are always recorded regardless of time.

## Dahua camera setup

1. Log in to the camera web interface
2. Go to **Setting > Event > IVS**, enable Smart Plan
3. Add a **Tripwire** rule across the road
4. Set direction and enable **Motor Vehicle** target filter
5. Set schedule to 24/7, click Apply

No alarm server configuration needed -- the app connects directly to the camera's event API.

## Intrusion events

The **Intrusions** tab shows intrusion detection events as a tiled gallery of snapshots. Events come from the camera's IVS event stream (rule name configured via `DAHUA_INTRUSION_IVS_NAME`).

JPG snapshots and DAV video recordings uploaded by the camera via FTP are matched to events by timestamp proximity. DAV files are converted to browser-friendly MP4 on first access using ffmpeg and cached in `/data/video_cache/`.

The media directory must be mounted read-only into the container at the path set by `INTRUSION_MEDIA_PATH` (default `/media/kamera_front`). See `docker-compose.yml` for the volume mount.
