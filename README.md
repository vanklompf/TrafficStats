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
| `DAHUA_IVS_NAMES` | Comma-separated IVS rule names to accept (empty = accept all) | `CarDetection` |
| `CITY` | City name for location (e.g. `Helsinki`, `London, UK`). Lat/lon and timezone are looked up automatically. | — |

When **CITY** is set, events are only recorded between sunrise and sunset at that location. The chart shows shaded bands for periods when no collection is done (night).

## Dahua camera setup

1. Log in to the camera web interface
2. Go to **Setting > Event > IVS**, enable Smart Plan
3. Add a **Tripwire** rule across the road
4. Set direction and enable **Motor Vehicle** target filter
5. Set schedule to 24/7, click Apply

No alarm server configuration needed -- the app connects directly to the camera's event API.
