# onvif-nvr-toolkit

A small, composable toolkit to turn one or more ONVIF IP cameras into a
24/7 recording NVR (Network Video Recorder), without depending on a
heavyweight surveillance suite.

The toolkit is built around the Unix philosophy: **six tiny, single-purpose
scripts that pipe into each other**, plus an optional **orchestrator daemon
(`nvrd`)** that runs the whole thing for you on a schedule.

```
onvif-discover → onvif-rtsp → go2rtc-gen
                            ↘ rtsp-play
                            ↘ rtsp-record → footage-merge
                            ↘ nvrd  (drives all of the above)
```

## Demo

`nvrd --check` against a real ONVIF camera (validates config + dependencies +
camera resolution, exits without recording):

![demo](demo.gif)

Watch with pause/seek on [asciinema.org](https://asciinema.org/a/sTpDdhgHc4xPd4G2).
Each leaf script's repo carries its own demo. See the table below for links.

## What's in the box

| Repo | One-line summary |
|---|---|
| [`onvif-discover`](https://github.com/SweatierKey/onvif-discover) | WS-Discovery probe; prints one device service URL per line. |
| [`onvif-rtsp`](https://github.com/SweatierKey/onvif-rtsp) | Resolve a device service URL to its RTSP URI (one URL in, one URL out). |
| [`go2rtc-gen`](https://github.com/SweatierKey/go2rtc-gen) | Generate a `go2rtc.yaml` from a list of RTSP URLs on stdin. |
| [`rtsp-play`](https://github.com/SweatierKey/rtsp-play) | Open an RTSP URL in mpv (preferred) or ffplay, tuned for low latency. |
| [`rtsp-record`](https://github.com/SweatierKey/rtsp-record) | Record an RTSP URL to disk in fixed-duration segments (no re-encode). |
| [`footage-merge`](https://github.com/SweatierKey/footage-merge) | Concatenate `rtsp-record` segments into a single playable file. |
| **this repo** | Hosts `nvrd`, the orchestrator that drives all the above, and `nvr-kiosk`, a tiny launcher that asks go2rtc for the current stream and exec's `mpv` fullscreen on it (no hard-coded camera URL in your kiosk unit). |

Each leaf repo is **standalone** and useful on its own. The umbrella adds
nothing to them at runtime — `nvrd` simply expects the six scripts to be in
your `PATH`.

## Why six scripts and not one big program

Because each step is independently testable, replaceable, and pipeable. You
can:

- Drop into `onvif-rtsp` to debug auth on a specific camera, without spinning
  up the whole pipeline.
- Replace `go2rtc-gen` with your own generator for a different consumer
  (e.g. a Frigate `config.yml`).
- Use `rtsp-play` standalone to verify a stream during installation.
- Call `footage-merge` against arbitrary `mkv`/`mp4` files outside the NVR loop.

The orchestrator (`nvrd`) is a thin layer that wires the same scripts
together on a schedule. It does not duplicate any of their logic — if a
camera works with `onvif-discover | onvif-rtsp | rtsp-record`, it works with
`nvrd`, and vice versa.

## Quick tour (no orchestrator)

The simplest way to record from a camera you already know the IP of:

```sh
onvif-rtsp --user admin --password admin --inject-credentials \
    http://192.168.0.73:8899/onvif/device_service \
  | rtsp-record -d 600 -o "/srv/footage/cam1-%Y-%m-%d_%H-%M-%S.mkv"
```

End-to-end pipeline (auto-discovery, single camera):

```sh
onvif-discover \
  | xargs -I{} onvif-rtsp --user admin --password admin --inject-credentials {} \
  | rtsp-record -d 600 -o "/srv/footage/%Y-%m-%d_%H-%M-%S.mkv"
```

Stitch a day's segments back into one file:

```sh
ls /srv/footage/2026-04-26_*.mkv | footage-merge -o /srv/footage/2026-04-26.mkv
```

## Quick tour (with `nvrd`)

`nvrd` is the long-running daemon that runs the same chain for you. It does
discovery, starts a [`go2rtc`](https://github.com/AlexxIT/go2rtc) proxy
(one connection per camera, instead of one per consumer), starts a
`rtsp-record` per camera, optionally opens `rtsp-play` in foreground,
monitors health, rotates files at midnight and merges the day's segments
automatically.

### Architecture

```
   ┌──────┐   1×RTSP    ┌────────┐   N×RTSP   ┌──────────────┐
   │ cam1 │ ──────────> │        │ ─────────> │ rtsp-record  │
   └──────┘             │ go2rtc │ ─────────> │ rtsp-play    │
                        │  :8554 │ ─────────> │ Frigate, HA, │
   ┌──────┐   1×RTSP    │        │            │ web UI, ...  │
   │ cam2 │ ──────────> │        │            └──────────────┘
   └──────┘             └────────┘
```

The cameras each see exactly one RTSP connection (from go2rtc), no matter
how many consumers tap the local proxy. Cheap firmware that reboots under
multiple concurrent connections stays happy.

```sh
# 1. install (this repo)
chmod +x nvrd
cp nvrd ~/.local/bin/

# 2. install the six leaf scripts (they must be in PATH)
for r in onvif-discover onvif-rtsp go2rtc-gen rtsp-play rtsp-record footage-merge; do
  curl -L -o ~/.local/bin/$r https://raw.githubusercontent.com/SweatierKey/$r/main/$r
  chmod +x ~/.local/bin/$r
done

# 3. install go2rtc (single static binary; needed unless proxy.mode=direct)
curl -L -o ~/.local/bin/go2rtc \
    https://github.com/AlexxIT/go2rtc/releases/latest/download/go2rtc_linux_amd64
chmod +x ~/.local/bin/go2rtc

# 4. install pip dep (PyYAML)
pip install --user PyYAML  # or: apt install python3-yaml

# 5. write a config
mkdir -p ~/.config/onvif-nvr
cp examples/config.yaml ~/.config/onvif-nvr/config.yaml
chmod 600 ~/.config/onvif-nvr/config.yaml   # contains the camera password
$EDITOR ~/.config/onvif-nvr/config.yaml

# 6. dry-run: validates config + dependencies + camera resolution,
# exits without recording. Always do this first.
nvrd --check

# 7. run
nvrd
```

> If you don't want go2rtc in your PATH, set `proxy.mode: direct` in the
> config — `nvrd` will skip starting it and have `rtsp-record`/`rtsp-play`
> connect to the cameras directly. Only do this if you're sure your camera
> can handle multiple concurrent RTSP clients.

Config example (full reference in [`examples/config.yaml`](examples/config.yaml)):

```yaml
storage:
  base_dir: /srv/footage

schedule:
  segment_duration: 600       # 10 minutes
  rotate_at: "00:00"          # local time

discovery:
  mode: auto                  # auto | static
  timeout: 5
  # for static mode, list cameras explicitly:
  # cameras:
  #   - name: cam1
  #     device_url: http://192.168.0.73:8899/onvif/device_service

auth:
  user: admin
  password: admin

proxy:
  mode: proxy                 # proxy | direct
  bind: 127.0.0.1             # change to 0.0.0.0 to share streams over LAN
  rtsp_port: 8554
  api_port: 1984

playback:
  enabled: false              # set to true to also open mpv/ffplay
  player: auto
  transport: tcp
  no_audio: true

logging:
  level: info
  file: ~/.local/state/nvrd.log   # set to null to log only to stdout
```

Files end up at `<base_dir>/<cam-name>/<YYYY-MM-DD>/cam-<...>.mkv`. At the
configured rotation time `nvrd` stops the day's `rtsp-record`, calls
`footage-merge` to produce `_merged-<YYYY-MM-DD>.mkv` next to the segments,
and starts a fresh `rtsp-record` for the new day.

The day folder name comes from `_dt.date.today()` at the moment the new
recording starts (i.e. just after the rotation tick). With the default
`rotate_at: "00:00"` that's intuitive: each folder holds one calendar
day's segments. **If you set `rotate_at` to anything else** (e.g.
`"03:00"` to push the merge work to the small hours), be aware that the
folder named `2026-04-27` then contains segments from 03:00 of that day
through 02:59 of `2026-04-28`. Filenames keep their wall-clock
timestamps either way.

See `nvrd --help` for command-line options.

## Kiosk display on boot (`nvr-kiosk`)

If the box is plugged into a TV and you want the camera fullscreen as
soon as it powers on, use `nvr-kiosk`. It asks the local `go2rtc` API
for the current stream name (so it survives a camera DHCP change or a
swap to a different camera) and then exec's `mpv` with low-latency,
fullscreen, looping flags.

A typical user-mode systemd unit (alongside `nvrd.service`):

```ini
[Unit]
Description=NVR kiosk display (cage + nvr-kiosk)
After=nvrd.service
Wants=nvrd.service
PartOf=nvrd.service

[Service]
Type=simple
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=XDG_RUNTIME_DIR=/run/user/%U
ExecStart=/usr/bin/cage -- %h/.local/bin/nvr-kiosk
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

`cage` is a tiny Wayland kiosk compositor; `nvr-kiosk` does the
discover-and-play. No camera URL is hard-coded anywhere.

If you have multiple cameras, point `nvr-kiosk --stream <name>` at the
one you want on screen. `nvr-kiosk --print-url` also exists if you want
the resolved RTSP URL on stdout (useful for shell scripting).

## Troubleshooting

**`onvif-discover` finds nothing on the LAN.**
- WS-Discovery uses UDP multicast (group `239.255.255.250`, port `3702`).
  Some routers / WiFi APs / WSL2 mirrored networking drop multicast
  silently. Test from a wired box first.
- Verify the cam is on the same subnet (multicast is link-local).
- Some cheap firmwares only respond to the second or third probe;
  `onvif-discover` 1.1.0+ already retransmits 3× — bump `--timeout` to
  10 if your network is slow.

**`onvif-rtsp` returns `wsse:Security must be understood…` or HTTP 401.**
- 401: wrong credentials (verify with the camera's web UI).
- "must be understood": should be fixed since `onvif-rtsp` 1.0; if you
  see it, the camera is replying to its own absent WS-Security handler —
  open an issue.

**`rtsp-record` keeps exiting with rc=4 and you get 0-byte files.**
- The cam's audio codec is incompatible with the chosen container.
  `nvrd` defaults to `.mkv` (Matroska) since 1.2.0 specifically because
  PCM µ-law audio (common on cheap ONVIF cams) cannot be muxed into MP4.
  If you have older `.mp4` segments lying around from a previous nvrd
  version, they are unrelated — just delete them.
- Run `rtsp-record -v rtsp://...` by hand to see ffmpeg's actual error.
  As of `nvrd` 1.3.0, ffmpeg's stderr is forwarded to nvrd's log
  automatically (look for `[cam-X] rtsp-record:` lines).

**`nvrd` loops "rtsp-record exited; restarting" forever.**
- Means a permanent failure. Check the last `[cam-X] rtsp-record:` lines
  in nvrd's log for the actual ffmpeg complaint.
- `nvrd` 1.3.0+ applies exponential backoff per cam (5s → 10s → … →
  60s) so the cam isn't hammered. Earlier versions retried every 30s
  forever.

**`go2rtc API not ready`.**
- Either go2rtc isn't in PATH (set `proxy.mode: direct` in config to
  skip it) or the configured `proxy.bind`/`proxy.api_port` is firewalled.
- Default bind is `127.0.0.1:1984` — should always work locally; if
  you've changed it, verify with `curl http://<bind>:<api_port>/api/streams`.

**Kiosk shows a black screen.**
- Run `cage -- mpv test.mkv` by hand to verify cage can grab the
  display (DRM/KMS or Wayland session).
- Check the journal: `journalctl --user -u nvr-kiosk.service`.
- `nvr-kiosk` 1.1.0+ catches `OSError` from `os.execv` and prints a
  one-line error if mpv can't be exec'd (wrong arch, bad shebang).

**Cam changes IP mid-day and recordings stop.**
- `nvrd` 1.3.0+ re-resolves cam URLs via `onvif-rtsp` whenever go2rtc
  dies, so a brief network blip recovers automatically. A *permanent*
  IP change with `discovery.mode: auto` will create a new cam folder
  (the cam name is derived from the IP). Use `discovery.mode: static`
  with explicit `name:` entries for production deployments where you
  want stable folders across IP churn.

**Config file group/world-readable warning at startup.**
- Your `config.yaml` is mode 644 (or worse). It contains the camera
  password. `chmod 600 ~/.config/onvif-nvr/config.yaml`.

## Design constraints (binding for `nvrd` and the leaf scripts)

These are the rules the whole toolkit respects. They exist mostly to make
the pieces predictable when chained together.

1. **Stdout = result. Stderr = everything else.** Leaf scripts that produce
   data put exactly that data on stdout — one URL per line, one config
   blob, etc. Logs, progress, errors all go to stderr.
2. **No surprise outputs.** A script that could write a file writes to
   stdout by default; redirect with `-o FILE`.
3. **GNU-style flags.** `-h/--help`, `-V/--version`, `-v/--verbose`,
   `-o/--output`, long options in `--kebab-case`.
4. **Documented exit codes:** `0` ok · `1` usage · `2` network · `3` auth ·
   `4` protocol · `130` Ctrl-C. Each script can add codes; they're
   documented in its `--help` and README.
5. **Lean dependencies.** Python stdlib + `requests` for HTTP-talkers,
   `PyYAML` only in `nvrd` (it actually needs YAML). No `python-onvif-zeep`,
   no `lxml`.
6. **Predictable failure modes.** Network/auth/protocol errors produce a
   one-line message on stderr and the right exit code — never a Python
   traceback for situations the script anticipates.
7. **Ctrl-C is graceful.** All scripts forward `SIGINT` to their child
   (ffmpeg, mpv, …) so in-flight files get finalised. Exit code `130`.
8. **Versioning.** Each script declares `VERSION = "X.Y.Z"`. SemVer; a
   change to stdout shape bumps the major.

For the original implementation specification — the contract each leaf
script must satisfy — see [`PROTOCOL.md`](PROTOCOL.md).

## Running the leaf scripts' tests

Every leaf repo carries its own `tests/` directory using only `unittest` and
runs offline. Clone any repo and:

```sh
python3 -m unittest discover tests/
```

The combined coverage (about 120 tests at the time of writing) takes a few
seconds and never touches the network.

## Repository layout

```
onvif-nvr-toolkit/
├── README.md            # this file
├── PROTOCOL.md          # the original spec; binding for new contributions
├── LICENSE              # MIT
├── nvrd                 # the orchestrator (no .py extension; chmod +x)
├── nvr-kiosk            # waits for a go2rtc stream then exec's mpv fullscreen
├── requirements.txt     # PyYAML
├── examples/
│   └── config.yaml      # annotated reference config for nvrd
└── tests/
    ├── test_nvrd.py
    └── test_nvr_kiosk.py
```

## License

MIT. See [`LICENSE`](LICENSE).
