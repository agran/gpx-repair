🇷🇺 [Русская версия](README.md)

# GPX Repair

Automatically repairs GPX tracks corrupted by a GPS jammer. It doesn't recover the real data, but it finds the corrupted section, removes it, and smoothly connects the start and end of the glitched segment.

## Web version

**https://agran.github.io/gpx-repair/**

Upload a GPX file right in your browser — no installation, no server.

## Python version

```bash
python fix_gpx_jammer.py input.gpx
python fix_gpx_jammer.py input.gpx output.gpx
```

### Parameters

| Parameter           | Default      | Description                                                                                                                                                                                                                                      |
| ------------------- | ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--profile`         | `hiking`     | Activity profile — sets defaults for the parameters below: `hiking`, `running`, `kayak`, `horse`, `mtb`, `road_bike`, `ski`, `enduro`, `boat`, `car`, `paraglider`, `train`. Explicitly passed flags below always override the profile's value   |
| `--max-speed`       | from profile | Maximum allowed speed; teleportation is detected above this                                                                                                                                                                                      |
| `--min-distance`    | from profile | Minimum distance of an anomalous jump                                                                                                                                                                                                            |
| `--pre-jitter-dist` | from profile | Remove drift points before the main jump                                                                                                                                                                                                         |
| `--max-vert-speed`  | from profile | Maximum allowed vertical speed                                                                                                                                                                                                                   |
| `--interval`        | from profile | Interpolation step between points                                                                                                                                                                                                                |
| `--no-interpolate`  | —            | Don't fill the gap, just remove it                                                                                                                                                                                                               |
| `--gap-fill`        | from profile | Gap fill method: `line` — simple offline join, `foot`/`bike`/`car` — route along roads and trails via OSRM (routing.openstreetmap.de) with terrain elevation from Open-Meteo. Requires internet; silently falls back to `line` on network errors |
| `--quiet`, `-q`     | —            | Don't print a detailed report                                                                                                                                                                                                                    |

## How it works

1. Finds points where the speed between neighboring coordinates is physically impossible (teleportation)
2. Determines the whole problematic section — from the first coordinate drift to the return to a normal trajectory
3. Additionally trims anomalous elevation jumps at the section's boundaries
4. Removes the problematic section and fills the gap — with a straight line or a route along roads/trails (`--gap-fill`)
