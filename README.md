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

[![asciicast](https://asciinema.org/a/sTpDdhgHc4xPd4G2.svg)](https://asciinema.org/a/sTpDdhgHc4xPd4G2)

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
| **this repo** | Hosts `nvrd`, the orchestrator that drives all the above. |

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
- Call `footage-merge` against arbitrary `mp4` files outside the NVR loop.

The orchestrator (`nvrd`) is a thin layer that wires the same scripts
together on a schedule. It does not duplicate any of their logic — if a
camera works with `onvif-discover | onvif-rtsp | rtsp-record`, it works with
`nvrd`, and vice versa.

## Quick tour (no orchestrator)

The simplest way to record from a camera you already know the IP of:

```sh
onvif-rtsp --user admin --password admin --inject-credentials \
    http://192.168.0.73:8899/onvif/device_service \
  | rtsp-record -d 600 -o "/srv/footage/cam1-%Y-%m-%d_%H-%M-%S.mp4"
```

End-to-end pipeline (auto-discovery, single camera):

```sh
onvif-discover \
  | xargs -I{} onvif-rtsp --user admin --password admin --inject-credentials {} \
  | rtsp-record -d 600 -o "/srv/footage/%Y-%m-%d_%H-%M-%S.mp4"
```

Stitch a day's segments back into one file:

```sh
ls /srv/footage/2026-04-26_*.mp4 | footage-merge -o /srv/footage/2026-04-26.mp4
```

## Quick tour (with `nvrd`)

`nvrd` is the long-running daemon that runs the same chain for you. It does
discovery, starts a `rtsp-record` per camera, optionally opens `rtsp-play`
in foreground, monitors health, rotates files at midnight and merges the
day's segments automatically.

```sh
# 1. install (this repo)
chmod +x nvrd
cp nvrd ~/.local/bin/

# 2. install the six leaf scripts (they must be in PATH)
for r in onvif-discover onvif-rtsp go2rtc-gen rtsp-play rtsp-record footage-merge; do
  curl -L -o ~/.local/bin/$r https://raw.githubusercontent.com/SweatierKey/$r/main/$r
  chmod +x ~/.local/bin/$r
done

# 3. write a config
mkdir -p ~/.config/onvif-nvr
cp examples/config.yaml ~/.config/onvif-nvr/config.yaml
$EDITOR ~/.config/onvif-nvr/config.yaml

# 4. run
nvrd
```

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

playback:
  enabled: false              # set to true to also open mpv/ffplay
  player: auto
  transport: tcp
  no_audio: true

logging:
  level: info
  file: ~/.local/state/nvrd.log   # set to null to log only to stdout
```

Files end up at `<base_dir>/<cam-name>/<YYYY-MM-DD>/cam-<...>.mp4`. At the
configured rotation time `nvrd` stops the day's `rtsp-record`, calls
`footage-merge` to produce `_merged-<YYYY-MM-DD>.mp4` next to the segments,
and starts a fresh `rtsp-record` for the new day.

See `nvrd --help` for command-line options.

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
├── requirements.txt     # PyYAML
├── examples/
│   └── config.yaml      # annotated reference config for nvrd
└── tests/
    └── test_nvrd.py     # offline test suite
```

## License

MIT. See [`LICENSE`](LICENSE).
