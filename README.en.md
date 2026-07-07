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

| Parameter           | Default | Description                                                  |
| ------------------- | ------- | -------------------------------------------------------------- |
| `--max-speed`       | 20 m/s  | Maximum allowed speed; teleportation is detected above this    |
| `--min-distance`    | 1000 m  | Minimum distance of an anomalous jump                          |
| `--pre-jitter-dist` | 200 m   | Remove drift points before the main jump                       |
| `--max-vert-speed`  | 5 m/s   | Maximum allowed vertical speed                                 |
| `--interval`        | 1 s     | Interpolation step between points                               |
| `--no-interpolate`  | —       | Don't fill the gap, just remove it                              |

## How it works

1. Finds points where the speed between neighboring coordinates is physically impossible (teleportation)
2. Determines the whole problematic section — from the first coordinate drift to the return to a normal trajectory
3. Additionally trims anomalous elevation jumps at the section's boundaries
4. Removes the problematic section and fills the gap with straight-line interpolation
