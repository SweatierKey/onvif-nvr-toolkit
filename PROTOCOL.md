# PROTOCOL — design contract for the toolkit

This document is the binding specification each leaf script of
`onvif-nvr-toolkit` satisfies. It exists so that any reimplementation,
fork or new tool added to the chain stays *predictable* when piped with
the others.

If you only want to use the toolkit, read [`README.md`](README.md) instead.
This document is for contributors and for anyone replacing one of the leaf
scripts with their own.

## The chain

```
onvif-discover → onvif-rtsp → go2rtc-gen
                            ↘ rtsp-play
                            ↘ rtsp-record → footage-merge
```

Each script lives in its own Git repo. Repos do not depend on each other
at code level: the only shared interface is the text format that flows
through the pipes — **one value per line, plain text on stdout, no
prefixes, no JSON, no colour**.

## Design philosophy (binding for every script)

1. **Strict Unix philosophy.** Each script does ONE thing. If you feel
   the urge to add a "convenient" feature outside the declared scope:
   don't — it belongs in a separate script or in a user-side wrapper.

2. **Stdout = bare result. Stderr = everything else.** When the user
   expects URLs, only URLs appear on stdout — one per line, no prefixes,
   no labels, no colours, no progress bars. Everything else (debug,
   progress, warnings, errors) goes to stderr.

3. **Scripts don't decide for the user.** If a script could write to a
   file, by default it writes to stdout and offers `-o FILE` to redirect.
   No "convenience" files appear without the user asking. No flags with
   "smart" defaults that change behaviour in non-obvious ways.

4. **Minimal output.** If a URL suffices, print a URL. No JSON wrappers,
   no headers, no courtesy blank lines.

5. **GNU-style flag conventions:**
   - `-h` / `--help`: help message, exit 0
   - `-V` / `--version`: print `<script-name> <version>`, exit 0
   - `-v` / `--verbose`: progress/debug on stderr
   - `-o FILE` / `--output FILE`: write the result to FILE instead of stdout
   - Long flags use `--kebab-case`
   - Short flags are mnemonic and don't collide with the standard ones

6. **Distinct, documented exit codes.** Don't reuse 1 for everything.
   Shared convention across the suite:
   - `0` success
   - `1` usage error (missing args, incompatible flags, unwritable file, …)
   - `2` network error (host unreachable, timeout, DNS, …)
   - `3` authentication failure
   - `4` protocol / unexpected-response error
   - `130` interrupted by Ctrl-C (standard convention)
   Each script may add other codes, but documents them in `--help` and
   in its README.

7. **Dependency check at start-up.** At the top of `main()`, before doing
   anything, each script verifies:
   - Required Python modules (with `try/except ImportError`)
   - Required system binaries (with `shutil.which`)
   When something is missing, print a single, actionable line on stderr
   (e.g. `ffmpeg not found in PATH (install with: apt install ffmpeg)`)
   and exit 1. No stack traces for predictable failures.

8. **No heavy libraries unless strictly necessary.** Prefer stdlib +
   `requests` over frameworks. In particular: do **not** use
   `python-onvif-zeep` (it pulls in `zeep` + `lxml` and is notoriously
   fragile against real cameras); the SOAP we need is short enough to
   write by hand.

9. **Robust on predictable errors.** Network timeouts, unreachable hosts,
   wrong credentials, malformed responses — all produce a one-line
   message on stderr and the right exit code. **No Python tracebacks for
   anything the script anticipates.** Tracebacks are only acceptable for
   actual bugs in the program.

10. **`KeyboardInterrupt` is handled.** Ctrl-C exits with 130 and prints
    `<script-name>: interrupted` on stderr. Never a traceback.

11. **No colour by default.** A script that wants to colour stderr does
    so only if `sys.stderr.isatty()` AND `NO_COLOR` is not set in the
    environment (see https://no-color.org).

12. **Versioning.** Each script declares `VERSION = "X.Y.Z"` at the top
    of the file. SemVer; a change to stdout shape bumps the major.

## Shared logging helpers

Every script defines (or imports) these two helpers (or equivalents):

```python
def log(verbose: bool, msg: str) -> None:
    if verbose:
        print(f"<script-name>: {msg}", file=sys.stderr)

def err(msg: str) -> None:
    print(f"<script-name>: {msg}", file=sys.stderr)
```

All stderr messages are prefixed with the script name (the way classic
GNU tools do: `ls: cannot access ...`).

## Repository layout

Each script lives in a standalone Git repo with this structure (adapted
where obvious):

```
<script-name>/
├── <script-name>            # executable Python (shebang, no .py extension)
├── README.md                # see below for required contents
├── LICENSE                  # MIT
├── .gitignore               # __pycache__, *.pyc, .venv, …
├── requirements.txt         # runtime deps only (e.g. requests). Empty if pure stdlib.
└── tests/
    └── test_<script_name>.py    # minimal tests with stdlib unittest
```

Notes:
- The executable does **not** carry a `.py` extension. It's meant to be
  copied into `~/.local/bin/` or `/usr/local/bin/`.
- `requirements.txt` lists runtime dependencies only. No dev deps unless
  needed. Pure-stdlib scripts have an empty (but present) file.
- Test file names use underscores — the script name uses hyphens, but
  Python module names cannot.
- Each README contains, in order:
  1. A one-line description
  2. **Install** section with `chmod +x` and copy-into-PATH
  3. **Usage** section with at least three examples: bare invocation,
     piped from the previous script in the chain, use of `-o`
  4. **Exit codes** section with the full table
  5. **Dependencies** section (Python and system)
  6. A line about its place in the chain (who feeds it, who it feeds)

## Per-script specifications

### 1. `onvif-discover`

**Behaviour:** discover ONVIF devices on the local network via WS-Discovery
multicast (UDP 3702, group `239.255.255.250`) and print one device service
URL per line on stdout, e.g.:

    http://192.168.1.64/onvif/device_service
    http://192.168.1.65:8000/onvif/device_service

Nothing else on stdout. One URL per line. Output sorted by IP.

### 2. `onvif-rtsp`

**Purpose:** given a single ONVIF device service URL, query it via SOAP
and print the RTSP URI of its first media profile. One URL in, one URL
out.

#### Input

- **Optional positional argument:** `DEVICE_URL` (e.g.
  `http://192.168.1.64/onvif/device_service`).
- **If the argument is omitted:** read ONE URL from stdin (the first
  non-empty line). This is the `onvif-discover | onvif-rtsp` use case.
- **If the argument is provided AND stdin is a pipe:** the argument
  wins (standard Unix-tool convention).
- **If the argument is omitted AND stdin is a terminal:** usage error
  (exit 1) with a clear message.

#### Flags

- `--user USER` — ONVIF username (default: empty = no auth)
- `--password PASSWORD` — ONVIF password (default: empty)
- `--inject-credentials` — opt-in: prepend URL-encoded `user:password@`
  to the returned RTSP URL. Requires `--user`/`--password`.
- `-o FILE` / `--output FILE` — write to FILE instead of stdout
- `-t SECONDS` / `--timeout SECONDS` — per-HTTP-request timeout
  (default: 10.0)
- `-v` / `--verbose` — progress on stderr
- `-V` / `--version`
- `-h` / `--help`

Validation:
- `--user` without `--password` (or vice versa): exit 1, clear message.
- URL not starting with `http://` or `https://`: exit 1.

#### SOAP sequence

1. **`GetCapabilities`** on the device service URL with
   `Category=Media`. Extract `tt:Media/tt:XAddr` — the media service
   URL (usually different from the device service, e.g.
   `http://192.168.1.64/onvif/Media`).

2. **`GetProfiles`** on the media service. Extract the `token` of the
   FIRST profile returned (`token` attribute of `trt:Profiles`). By ONVIF
   convention the first profile is the main stream — we don't choose, we
   take what the device gives us.

3. **`GetStreamUri`** on the media service with:
   - `StreamSetup/Stream = RTP-Unicast`
   - `StreamSetup/Transport/Protocol = RTSP`
   - `ProfileToken = <token from step 2>`
   Extract `tt:Uri`. That's the RTSP URL we print.

The only thing on stdout is the RTSP URL, followed by a newline.

#### Authentication

ONVIF uses WS-Security UsernameToken with PasswordDigest (SHA-1). When
`--user` and `--password` are both provided, every SOAP request carries
a `<wsse:Security>` header containing:

- `<wsse:Username>` in cleartext
- `<wsse:Password Type="...#PasswordDigest">` =
  base64(SHA1(nonce + created + password))
- `<wsse:Nonce EncodingType="...#Base64Binary">` = base64(16 random bytes)
- `<wsu:Created>` = ISO 8601 UTC timestamp
  (e.g. `2026-04-26T15:14:00.123456Z`)

Nonce and `Created` are regenerated for every request — never reused.

> **Implementation note.** Do **not** add `s:mustUnderstand="1"` to the
> Security element. Many gSOAP-based camera firmwares (real-world test:
> a generic HS-Camera/IPCAM) reject the request with
> `wsse:Security must be understood but cannot be handled` even when
> they happily authenticate the same token without the attribute.
> `mustUnderstand` is not required by this spec; omitting it is more
> compatible.

When user/password are empty, no security header is sent. Many cameras
allow `GetCapabilities`/`GetProfiles` anonymously but refuse
`GetStreamUri` without credentials — in that case the device responds
with a SOAP Fault `NotAuthorized` or HTTP 401 and we treat it as an
auth error (exit 3).

#### Error handling (full mapping)

| Situation | stderr | exit |
|---|---|---|
| Argument missing and stdin is tty | `no device URL given and stdin is a terminal` | 1 |
| Empty stdin | `no device URL on stdin` | 1 |
| Malformed URL | `not an HTTP(S) URL: <url>` | 1 |
| `--user` without `--password` (or vice versa) | clear message | 1 |
| `-o` file not writable | `could not write <file>: <reason>` | 1 |
| Connection refused / no route | `could not connect to <url>: <reason>` | 2 |
| Connection or request timeout | `connection timed out: <url>` or `request timed out: <url>` | 2 |
| HTTP 401 with no SOAP body | `authentication failed: device returned HTTP 401` | 3 |
| SOAP Fault subcode `NotAuthorized` or reason containing "auth" | `authentication failed: <fault reason>` | 3 |
| Non-XML response on non-401 HTTP | `server returned non-XML response (HTTP <code>)` | 4 |
| Other SOAP Fault | `SOAP fault: <fault reason>` | 4 |
| Valid SOAP, missing `Media/XAddr` | `device did not return a Media service address` | 4 |
| `GetProfiles` returns no profiles | `device returned no media profiles` | 4 |
| First profile lacks `token` attribute | `first media profile has no token attribute` | 4 |
| `GetStreamUri` returns no `Uri` | `device did not return an RTSP URI` | 4 |
| Other non-2xx HTTP without fault | `HTTP <code> from <url>` | 4 |

### 3. `go2rtc-gen`

**Purpose:** generate a `go2rtc.yaml` from a list of RTSP URLs on stdin
(one per line).

#### Input

- Stdin: one RTSP URL per line. Blank lines are ignored. Lines not
  starting with `rtsp://` (case-insensitive) emit a stderr warning and
  are skipped (final exit 0 if at least one URL was processed,
  otherwise exit 4).

#### Flags

- `-o FILE` / `--output FILE` — write YAML to FILE instead of stdout
- `--name-prefix PREFIX` — stream-name prefix (default `cam`).
  Streams are named `<prefix>1`, `<prefix>2`, …
- `--api-listen ADDR` — go2rtc `api.listen` (default: `:1984`)
- `--rtsp-listen ADDR` — go2rtc `rtsp.listen` (default: `:8554`)
- `-v` / `--verbose`, `-V` / `--version`, `-h` / `--help`

#### Output (example, two URLs)

```yaml
api:
  listen: ":1984"
rtsp:
  listen: ":8554"
streams:
  cam1: rtsp://192.168.1.64:554/Streaming/Channels/101
  cam2: rtsp://192.168.1.65:554/Streaming/Channels/101
```

Order matches arrival order on stdin. URLs containing characters
reserved by YAML (`@`, `#`, `&`, `*`, …) are emitted as double-quoted
scalars; plain ones are not quoted.

#### Dependencies

Stdlib only. No `pyyaml` — the format is simple enough to emit by hand
and that saves a dependency.

#### Errors

- Empty stdin → `no RTSP URLs on stdin`, exit 4
- All lines rejected → `no valid RTSP URLs on stdin`, exit 4
- `-o` file not writable → exit 1

### 4. `rtsp-play`

**Purpose:** open an RTSP URL in a local viewer for inspection. Wraps
**mpv** (preferred) or **ffplay** (fallback), tuned for low-latency live
playback.

#### Input

- Optional positional `RTSP_URL`, or one URL from stdin (same logic as
  `onvif-rtsp`).

#### Flags

- `--player {auto,mpv,ffplay}` — default `auto` (mpv if present, else
  ffplay)
- `--transport {tcp,udp}` — default `tcp` (more reliable on LAN)
- `--no-audio` — disable audio
- `-v` / `--verbose`, `-V` / `--version`, `-h` / `--help`

#### Behaviour

- Verifies at start-up that the chosen player is in PATH; otherwise
  exit 1 with a clear install hint.
- Launches the player with low-latency tuning:
  - **mpv**: `--profile=low-latency`, plus `probesize=32`,
    `analyzeduration=0`, `cache=no`, `framedrop=decoder+vo`, and
    `+discardcorrupt`. RTSP transport layered with
    `--demuxer-lavf-o-add` (assignment form clobbers the profile —
    don't use it).
  - **ffplay**: `-fflags nobuffer+discardcorrupt`, `-flags low_delay`,
    `-probesize 32`, `-analyzeduration 0`, `-framedrop`,
    `-rtsp_transport <t>`.
- On Ctrl-C, propagates the signal to the player and exits 130.
- Exit code is the player's own (besides the standard exceptions
  above).

#### Output

- Nothing on stdout. The script does not produce text data.
- Player stderr is sent to `/dev/null` in non-verbose mode (mpv and
  ffplay are both chatty by default). With `-v`, it passes through.

### 5. `rtsp-record`

**Purpose:** record an RTSP URL to disk in fixed-duration segments.
Wraps `ffmpeg` with the `segment` muxer.

#### Input

- Optional positional `RTSP_URL`, or one URL from stdin (same logic as
  above).

#### Flags

- `-d SECONDS` / `--duration SECONDS` — segment length in seconds
  (default: 600 = 10 minutes)
- `-o PATTERN` / `--output PATTERN` — filename pattern with `strftime`
  placeholders (default: `recording-%Y-%m-%d_%H-%M-%S.mp4`).
  **Must contain at least one strftime placeholder**, otherwise
  segments would overwrite each other — exit 1 if missing.
- `--transport {tcp,udp}` — default `tcp`
- `--max-segments N` — stop after N segments (default 0 = run
  indefinitely)
- `-v` / `--verbose`, `-V` / `--version`, `-h` / `--help`

#### Behaviour

- Verifies `ffmpeg` is in PATH; otherwise exit 1 with install hint.
- Uses ffmpeg's `segment` muxer:
  ```
  ffmpeg -rtsp_transport <t> -i <url> -c copy -f segment \
         -segment_time <d> -strftime 1 -reset_timestamps 1 <pattern>
  ```
- `-c copy` — never re-encode (preserves quality and CPU).
- On Ctrl-C: forwards `SIGINT` to ffmpeg (NOT `SIGKILL`) so the
  in-flight segment is finalised cleanly. Exit 130.
- Detects new segments by parsing ffmpeg's stderr `Opening '...' for
  writing` lines. With `--max-segments N`, sends `SIGINT` when segment
  N+1 opens; you may see a small trailing partial file in addition to
  the N completed ones.
- In `-v`, logs each new segment to stderr with our prefix; everything
  else from ffmpeg also passes through.
- **Stdout stays empty** — this script does not produce data for the
  pipeline.

#### Errors

- Pattern without placeholder → exit 1 with
  `output pattern must contain a strftime placeholder (e.g. %Y%m%d-%H%M%S)`
- Output directory does not exist → exit 1
- ffmpeg exits non-zero → exit 4 with the last ffmpeg message

### 6. `footage-merge`

**Purpose:** concatenate video files (the segments produced by
`rtsp-record`) into one output. Wraps `ffmpeg`'s concat demuxer.

#### Input

- Positional arguments: list of video files, OR
- Stdin: one path per line (for `ls *.mp4 | footage-merge -o out.mp4`)
- Mixing the two modes (positional + real stdin pipe) is rejected with
  a clear message. `/dev/null`, ttys and inherited stdins are not
  considered "stdin input", so cron/systemd usage does not trigger the
  check (detection uses `os.fstat()` against `S_ISFIFO`/`S_ISSOCK`).

#### Flags

- `-o FILE` / `--output FILE` — output file (**mandatory** — binary
  content is never written to stdout).
- `-f` / `--force` — overwrite an existing output file.
- `--reencode` — re-encode (default: `-c copy`). Needed when the
  inputs disagree on codec/resolution.
- `-v` / `--verbose`, `-V` / `--version`, `-h` / `--help`

#### Behaviour

- Verifies `ffmpeg` is in PATH.
- Verifies all input files exist and are readable **before** invoking
  ffmpeg. If any are missing, exits 1 listing them.
- Writes the concat list to a `tempfile.NamedTemporaryFile` and
  removes it in a `try/finally`, even on error.
- Command:
  ```
  ffmpeg -f concat -safe 0 -i <listfile> -c copy <output>
  ```
  (without `-c copy` if `--reencode`).
- ffmpeg progress on stderr only with `-v`.
- On success, with `-v`, prints the output path and size.

#### Errors

- Output exists → exit 1 with `output file exists: <path> (refusing to
  overwrite; use -f to override)`. `-f`/`--force` overrides.
- ffmpeg fails → exit 4

## The orchestrator (`nvrd`)

`nvrd` ships with this umbrella repo. It is *not* a leaf script and not
part of the data pipeline — it's a long-running coordinator that calls
the leaf scripts on a schedule. See [`README.md`](README.md) for usage.

The orchestrator binds itself to the same conventions as the leaf
scripts (stderr for logs, GNU flags, exit codes, predictable failures).
It does not duplicate any logic — it just shells out to the leaf
scripts that must exist in `PATH`.
