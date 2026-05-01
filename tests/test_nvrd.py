"""Tests for nvrd. Pure-function and CLI tests, with leaf scripts mocked
via PATH. The daemon's main loop and CameraWorker thread are deliberately
not exercised end-to-end — they would require real subprocess plumbing,
threading and clock manipulation. Building blocks are tested instead."""

import datetime as _dt
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "nvrd"


def _load_module():
    loader = SourceFileLoader("nvrd_mod", str(SCRIPT))
    spec = importlib.util.spec_from_loader("nvrd_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    # @dataclasses.dataclass needs the module to be registered before
    # decoration runs.
    sys.modules["nvrd_mod"] = mod
    loader.exec_module(mod)
    return mod


nvrd = _load_module()


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------

class ParseRotateAtTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(nvrd.parse_rotate_at("00:00"), (0, 0))
        self.assertEqual(nvrd.parse_rotate_at("23:59"), (23, 59))
        self.assertEqual(nvrd.parse_rotate_at("9:30"), (9, 30))


class NextRotationAfterTests(unittest.TestCase):
    def test_today_in_future(self):
        now = _dt.datetime(2026, 4, 26, 14, 0, 0)
        self.assertEqual(
            nvrd.next_rotation_after(now, 18, 0),
            _dt.datetime(2026, 4, 26, 18, 0, 0),
        )

    def test_today_in_past_rolls_to_tomorrow(self):
        now = _dt.datetime(2026, 4, 26, 23, 0, 0)
        self.assertEqual(
            nvrd.next_rotation_after(now, 0, 0),
            _dt.datetime(2026, 4, 27, 0, 0, 0),
        )

    def test_exact_time_rolls_to_tomorrow(self):
        # equality counts as past so the daemon doesn't trigger a second time.
        now = _dt.datetime(2026, 4, 26, 0, 0, 0)
        self.assertEqual(
            nvrd.next_rotation_after(now, 0, 0),
            _dt.datetime(2026, 4, 27, 0, 0, 0),
        )


class SafeNameFromUrlTests(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(
            nvrd._safe_name_from_url("http://192.168.0.73:8899/onvif/device_service"),
            "cam-192-168-0-73",
        )

    def test_hostname(self):
        self.assertEqual(
            nvrd._safe_name_from_url("http://camera.local/onvif/device_service"),
            "cam-camera-local",
        )

    def test_unknown(self):
        self.assertEqual(
            nvrd._safe_name_from_url("not-a-url"),
            "cam-unknown",
        )


class SegmentHelpersTests(unittest.TestCase):
    def test_segment_files_filtered_and_sorted(self):
        with tempfile.TemporaryDirectory() as d:
            dp = Path(d)
            (dp / "cam-2026-04-26_18-30-01.mkv").write_text("x")
            (dp / "cam-2026-04-26_18-20-01.mkv").write_text("x")
            (dp / "_merged-2026-04-26.mkv").write_text("x")  # excluded
            (dp / "notes.txt").write_text("x")               # excluded
            (dp / "subdir").mkdir()                          # excluded
            segs = nvrd._segment_files_in(dp)
            self.assertEqual(
                [p.name for p in segs],
                ["cam-2026-04-26_18-20-01.mkv", "cam-2026-04-26_18-30-01.mkv"],
            )

    def test_latest_segment(self):
        with tempfile.TemporaryDirectory() as d:
            dp = Path(d)
            (dp / "cam-2026-04-26_18-30-01.mkv").write_text("x")
            (dp / "cam-2026-04-26_18-40-01.mkv").write_text("x")
            self.assertEqual(
                nvrd._latest_segment(dp).name,
                "cam-2026-04-26_18-40-01.mkv",
            )

    def test_latest_segment_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(nvrd._latest_segment(Path(d)))


class DeepMergeTests(unittest.TestCase):
    def test_recursive(self):
        dst = {"a": {"x": 1, "y": 2}, "b": 1}
        src = {"a": {"y": 99, "z": 3}, "c": 4}
        self.assertEqual(
            nvrd._deep_merge(dst, src),
            {"a": {"x": 1, "y": 99, "z": 3}, "b": 1, "c": 4},
        )

    def test_overwrites_non_dict(self):
        dst = {"a": [1, 2, 3]}
        src = {"a": [9]}
        self.assertEqual(nvrd._deep_merge(dst, src), {"a": [9]})


class RedactUrlTests(unittest.TestCase):
    def test_rtsp_userinfo_replaced(self):
        self.assertEqual(
            nvrd._redact_url("rtsp://admin:hunter2@192.168.1.4/stream"),
            "rtsp://***@192.168.1.4/stream",
        )

    def test_rtsps_userinfo_replaced(self):
        self.assertEqual(
            nvrd._redact_url("rtsps://u:p@host:443/x"),
            "rtsps://***@host:443/x",
        )

    def test_http_userinfo_replaced(self):
        self.assertEqual(
            nvrd._redact_url("http://u:p@cam/onvif"),
            "http://***@cam/onvif",
        )

    def test_url_without_userinfo_unchanged(self):
        self.assertEqual(
            nvrd._redact_url("rtsp://192.168.1.4:8554/cam-1"),
            "rtsp://192.168.1.4:8554/cam-1",
        )

    def test_multiple_urls_in_one_string(self):
        # The whole go2rtc.yaml gets redacted in one shot.
        s = ('streams:\n'
             '  cam1: "rtsp://admin:s3cret@1.2.3.4/main"\n'
             '  cam2: "rtsp://admin:other@5.6.7.8/main"\n')
        out = nvrd._redact_url(s)
        self.assertNotIn("s3cret", out)
        self.assertNotIn("other", out)
        self.assertIn("rtsp://***@1.2.3.4/main", out)
        self.assertIn("rtsp://***@5.6.7.8/main", out)


class DefaultConfigTests(unittest.TestCase):
    def test_each_call_returns_fresh_dict(self):
        a = nvrd._default_config()
        b = nvrd._default_config()
        a["schedule"]["segment_duration"] = 999
        self.assertEqual(b["schedule"]["segment_duration"], 600)

    def test_default_config_path_uses_xdg(self):
        env = {"XDG_CONFIG_HOME": "/var/myxdg"}
        original = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = "/var/myxdg"
        try:
            self.assertEqual(nvrd._default_config_path(),
                             "/var/myxdg/onvif-nvr/config.yaml")
        finally:
            if original is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = original

    def test_default_config_path_falls_back_to_home(self):
        original = os.environ.pop("XDG_CONFIG_HOME", None)
        try:
            p = nvrd._default_config_path()
            self.assertTrue(p.endswith("/onvif-nvr/config.yaml"), msg=p)
            self.assertNotIn("$XDG", p)
        finally:
            if original is not None:
                os.environ["XDG_CONFIG_HOME"] = original


class WarnIfWorldReadableTests(unittest.TestCase):
    def test_warns_on_644(self):
        import io
        if os.geteuid() == 0:
            self.skipTest("running as root — perm bits semantics differ")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.yaml"
            p.write_text("auth:\n  password: secret\n")
            os.chmod(p, 0o644)
            buf = io.StringIO()
            old_stderr, sys.stderr = sys.stderr, buf
            try:
                nvrd._warn_if_config_world_readable(p)
            finally:
                sys.stderr = old_stderr
            self.assertIn("WARNING", buf.getvalue())
            self.assertIn("chmod 600", buf.getvalue())

    def test_silent_on_600(self):
        import io
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.yaml"
            p.write_text("auth:\n  password: secret\n")
            os.chmod(p, 0o600)
            buf = io.StringIO()
            old_stderr, sys.stderr = sys.stderr, buf
            try:
                nvrd._warn_if_config_world_readable(p)
            finally:
                sys.stderr = old_stderr
            self.assertEqual(buf.getvalue(), "")


# ---------------------------------------------------------------------------
# Config loading & validation
# ---------------------------------------------------------------------------

class LoadConfigTests(unittest.TestCase):
    def test_no_path_returns_defaults(self):
        cfg = nvrd.load_config(None)
        self.assertEqual(cfg["schedule"]["segment_duration"], 600)
        self.assertEqual(cfg["discovery"]["mode"], "auto")
        # Mutating the returned config must not contaminate further calls.
        cfg["schedule"]["segment_duration"] = 1
        cfg2 = nvrd.load_config(None)
        self.assertEqual(cfg2["schedule"]["segment_duration"], 600)

    def test_partial_overrides_merge_with_defaults(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.yaml")
            with open(p, "w") as f:
                f.write("schedule:\n  segment_duration: 30\n")
            cfg = nvrd.load_config(p)
            self.assertEqual(cfg["schedule"]["segment_duration"], 30)
            # Defaults preserved
            self.assertEqual(cfg["schedule"]["rotate_at"], "00:00")
            self.assertEqual(cfg["discovery"]["mode"], "auto")

    def test_bad_segment_duration_rejected(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.yaml")
            with open(p, "w") as f:
                f.write("schedule:\n  segment_duration: -1\n")
            with self.assertRaises(SystemExit):
                nvrd.load_config(p)

    def test_bad_rotate_at_rejected(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.yaml")
            with open(p, "w") as f:
                f.write('schedule:\n  rotate_at: "25:00"\n')
            with self.assertRaises(SystemExit):
                nvrd.load_config(p)

    def test_static_mode_without_cameras_rejected(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.yaml")
            with open(p, "w") as f:
                f.write("discovery:\n  mode: static\n  cameras: []\n")
            with self.assertRaises(SystemExit):
                nvrd.load_config(p)


# ---------------------------------------------------------------------------
# Leaf-script wrappers (with PATH-mocked binaries)
# ---------------------------------------------------------------------------

class _MockLeavesPath:
    """Install fake leaf scripts in PATH. Each fake script:
    - reads its name and argv
    - records arguments to a per-script log file
    - prints whatever is in <name>.stdout (if it exists), exits with rc from <name>.rc"""

    def __init__(self, scripts):
        self.scripts = scripts
        self.dir = None
        self.old = None

    def __enter__(self):
        self.dir = tempfile.mkdtemp()
        for name in self.scripts:
            sp = os.path.join(self.dir, name)
            log = os.path.join(self.dir, f"{name}.log")
            stdout = os.path.join(self.dir, f"{name}.stdout")
            rc = os.path.join(self.dir, f"{name}.rc")
            with open(sp, "w") as f:
                f.write(textwrap.dedent(f"""\
                    #!/bin/sh
                    {{ printf "%s\\n" "$@"; }} > "{log}"
                    if [ -f "{stdout}" ]; then cat "{stdout}"; fi
                    if [ -f "{rc}" ]; then exit "$(cat "{rc}")"; fi
                    exit 0
                    """))
            os.chmod(sp, 0o755)
        self.old = os.environ.get("PATH")
        os.environ["PATH"] = self.dir + os.pathsep + (self.old or "")
        return self

    def set_stdout(self, name, text):
        with open(os.path.join(self.dir, f"{name}.stdout"), "w") as f:
            f.write(text)

    def set_rc(self, name, rc):
        with open(os.path.join(self.dir, f"{name}.rc"), "w") as f:
            f.write(str(rc))

    def args(self, name):
        with open(os.path.join(self.dir, f"{name}.log")) as f:
            return f.read().splitlines()

    def __exit__(self, *exc):
        os.environ["PATH"] = self.old or ""
        shutil.rmtree(self.dir, ignore_errors=True)


class DiscoverDevicesTests(unittest.TestCase):
    def test_returns_lines_and_strips_blanks(self):
        with _MockLeavesPath(["onvif-discover"]) as mp:
            mp.set_stdout("onvif-discover",
                          "http://1.1.1.1/x\n\nhttp://2.2.2.2/y\n")
            urls = nvrd.discover_devices(timeout=2)
            self.assertEqual(urls, ["http://1.1.1.1/x", "http://2.2.2.2/y"])
            self.assertIn("-t", mp.args("onvif-discover"))

    def test_failure_raises(self):
        with _MockLeavesPath(["onvif-discover"]) as mp:
            mp.set_rc("onvif-discover", 2)
            with self.assertRaises(SystemExit):
                nvrd.discover_devices(timeout=2)


class ResolveRtspUrlTests(unittest.TestCase):
    def test_happy_path_with_credentials_via_env(self):
        # nvrd 1.3.0+ passes ONVIF creds via env vars, not CLI flags, so
        # they don't appear in /proc/<pid>/cmdline. Verify both: the env is
        # set, and --user is NOT in argv.
        with _MockLeavesPath(["onvif-rtsp"]) as mp:
            mp.set_stdout("onvif-rtsp", "rtsp://u:p@1.2.3.4/s\n")
            # The mock leaf script is replaced with a custom one that
            # records its env too.
            sp = os.path.join(mp.dir, "onvif-rtsp")
            envlog = os.path.join(mp.dir, "onvif-rtsp.env")
            with open(sp, "w") as f:
                f.write(textwrap.dedent(f"""\
                    #!/bin/sh
                    {{ printf "%s\\n" "$@"; }} > "{mp.dir}/onvif-rtsp.log"
                    {{ printf "ONVIF_USER=%s\\nONVIF_PASSWORD=%s\\n" \
                        "$ONVIF_USER" "$ONVIF_PASSWORD"; }} > "{envlog}"
                    cat "{mp.dir}/onvif-rtsp.stdout"
                    """))
            os.chmod(sp, 0o755)
            cam = nvrd.Camera(
                name="cam1",
                device_url="http://1.2.3.4/onvif/device_service",
                user="admin", password="hunter2",
                inject_credentials=True,
            )
            url = nvrd.resolve_rtsp_url(cam, timeout=5)
            self.assertEqual(url, "rtsp://u:p@1.2.3.4/s")
            args = mp.args("onvif-rtsp")
            self.assertNotIn("--user", args)
            self.assertNotIn("hunter2", args)
            self.assertIn("--inject-credentials", args)
            self.assertEqual(args[-1], "http://1.2.3.4/onvif/device_service")
            with open(envlog) as f:
                env = f.read()
            self.assertIn("ONVIF_USER=admin", env)
            self.assertIn("ONVIF_PASSWORD=hunter2", env)

    def test_no_credentials_no_env_set(self):
        # When the cam has no credentials, nvrd must scrub any inherited
        # ONVIF_USER/ONVIF_PASSWORD from the env so a previous cam's creds
        # don't leak.
        with _MockLeavesPath(["onvif-rtsp"]) as mp:
            mp.set_stdout("onvif-rtsp", "rtsp://anon/x\n")
            sp = os.path.join(mp.dir, "onvif-rtsp")
            envlog = os.path.join(mp.dir, "onvif-rtsp.env")
            with open(sp, "w") as f:
                f.write(textwrap.dedent(f"""\
                    #!/bin/sh
                    {{ printf "%s\\n" "$@"; }} > "{mp.dir}/onvif-rtsp.log"
                    {{ printf "ONVIF_USER=%s\\nONVIF_PASSWORD=%s\\n" \
                        "$ONVIF_USER" "$ONVIF_PASSWORD"; }} > "{envlog}"
                    cat "{mp.dir}/onvif-rtsp.stdout"
                    """))
            os.chmod(sp, 0o755)
            os.environ["ONVIF_USER"] = "leaked"
            os.environ["ONVIF_PASSWORD"] = "leaked"
            try:
                cam = nvrd.Camera(
                    name="anon", device_url="http://1.2.3.4/onvif/device_service",
                    user="", password="", inject_credentials=False,
                )
                url = nvrd.resolve_rtsp_url(cam, timeout=5)
            finally:
                os.environ.pop("ONVIF_USER", None)
                os.environ.pop("ONVIF_PASSWORD", None)
            self.assertEqual(url, "rtsp://anon/x")
            self.assertNotIn("--user", mp.args("onvif-rtsp"))
            with open(envlog) as f:
                env = f.read()
            self.assertIn("ONVIF_USER=\n", env)
            self.assertIn("ONVIF_PASSWORD=\n", env)

    def test_failure_raises(self):
        with _MockLeavesPath(["onvif-rtsp"]) as mp:
            mp.set_rc("onvif-rtsp", 3)
            cam = nvrd.Camera(
                name="bad", device_url="http://1.2.3.4/onvif/device_service",
                user="admin", password="wrong", inject_credentials=True,
            )
            with self.assertRaises(RuntimeError):
                nvrd.resolve_rtsp_url(cam, timeout=5)


class MergeSegmentsTests(unittest.TestCase):
    def test_invokes_footage_merge_with_outputs(self):
        with _MockLeavesPath(["footage-merge"]) as mp, \
             tempfile.TemporaryDirectory() as d:
            inputs = [Path(d) / f"cam-2026-04-26_{i:02d}-00-00.mkv"
                      for i in range(1, 3)]
            for p in inputs:
                p.write_text("x")
            out = Path(d) / "_merged-2026-04-26.mkv"
            nvrd.merge_segments(inputs, out)
            args = mp.args("footage-merge")
            self.assertIn("-o", args)
            self.assertEqual(args[args.index("-o") + 1], str(out))
            for inp in inputs:
                self.assertIn(str(inp), args)


# ---------------------------------------------------------------------------
# Orchestrator.gather_cameras (config -> Camera list)
# ---------------------------------------------------------------------------

class PipeStderrToLoggerTests(unittest.TestCase):
    """The orchestrator forwards rtsp-record's stderr to its own logger
    (issue #4 — without this, all ffmpeg diagnostics are lost). Verify
    the splitter and the warning/info routing."""

    class _FakeProc:
        def __init__(self, lines):
            self._lines = lines
        @property
        def stderr(self):
            return iter(self._lines)

    def _capture(self, lines):
        import io
        # Attach a temporary handler that captures records by level.
        buf_warn = []
        buf_info = []
        class _Handler(__import__("logging").Handler):
            def emit(self, record):
                if record.levelname == "WARNING":
                    buf_warn.append(record.getMessage())
                elif record.levelname == "INFO":
                    buf_info.append(record.getMessage())
        h = _Handler()
        nvrd.logger.addHandler(h)
        old_level = nvrd.logger.level
        nvrd.logger.setLevel(__import__("logging").DEBUG)
        try:
            nvrd._pipe_stderr_to_logger(self._FakeProc(lines), "[X] rtsp-record")
        finally:
            nvrd.logger.removeHandler(h)
            nvrd.logger.setLevel(old_level)
        return buf_info, buf_warn

    def test_error_lines_routed_to_warning(self):
        info, warn = self._capture([
            "rtsp-record: segment 1: foo.mkv\n",
            "[rtsp @ 0x...] method DESCRIBE failed: 401 Unauthorized\n",
            "Conversion failed!\n",
        ])
        # The 401 line and the failure line must hit WARNING.
        self.assertTrue(any("401 Unauthorized" in m for m in warn))
        self.assertTrue(any("failed" in m.lower() for m in warn))
        # The mundane "segment 1" line is INFO.
        self.assertTrue(any("segment 1" in m for m in info))

    def test_handles_blank_lines(self):
        info, warn = self._capture(["\n", "  \n", "real line\n"])
        self.assertEqual(len(warn), 0)
        self.assertEqual(len(info), 1)


class Go2rtcSupervisorSetCamsTests(unittest.TestCase):
    """Verify that set_cams() actually replaces what the next start() will
    write into the /tmp config — guards against #6 (stale-config restart)."""

    def _cfg(self):
        return {"proxy": {"mode": "proxy", "bind": "127.0.0.1",
                          "rtsp_port": 8554, "api_port": 1984,
                          "ready_timeout": 1}}

    def test_set_cams_replaces_internal_list(self):
        c1 = nvrd.Camera(name="cam-x", device_url="http://x/o",
                         user="", password="", inject_credentials=False,
                         rtsp_url="rtsp://OLD/x")
        sup = nvrd.Go2rtcSupervisor([c1], self._cfg())
        c2 = nvrd.Camera(name="cam-x", device_url="http://x/o",
                         user="", password="", inject_credentials=False,
                         rtsp_url="rtsp://NEW/x")
        sup.set_cams([c2])
        # The yaml that would be written if we started now:
        rendered = nvrd.render_go2rtc_yaml(sup.cams, "127.0.0.1", 8554, 1984)
        self.assertIn("rtsp://NEW/x", rendered)
        self.assertNotIn("rtsp://OLD/x", rendered)


class RenderGo2rtcYamlTests(unittest.TestCase):
    def _cams(self, *names_and_urls):
        return [nvrd.Camera(name=n, device_url=u, user="", password="",
                            inject_credentials=False, rtsp_url=u)
                for n, u in names_and_urls]

    def test_basic_layout(self):
        cams = self._cams(
            ("cam-front", "rtsp://1.1.1.1/main"),
            ("cam-back",  "rtsp://2.2.2.2/main"),
        )
        out = nvrd.render_go2rtc_yaml(cams, "127.0.0.1", 8554, 1984)
        self.assertEqual(
            out,
            "api:\n"
            "  listen: 127.0.0.1:1984\n"
            "rtsp:\n"
            "  listen: 127.0.0.1:8554\n"
            "streams:\n"
            "  cam-front: rtsp://1.1.1.1/main\n"
            "  cam-back: rtsp://2.2.2.2/main\n",
        )

    def test_url_with_credentials_quoted(self):
        cams = self._cams(("cam1", "rtsp://u:p@host/x"))
        out = nvrd.render_go2rtc_yaml(cams, "127.0.0.1", 8554, 1984)
        self.assertIn('cam1: "rtsp://u:p@host/x"', out)

    def test_bind_zero_addr(self):
        out = nvrd.render_go2rtc_yaml([], "0.0.0.0", 8554, 1984)
        self.assertIn("listen: 0.0.0.0:1984", out)
        self.assertIn("listen: 0.0.0.0:8554", out)


class ProxyConfigValidationTests(unittest.TestCase):
    def _load(self, body):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.yaml")
            with open(p, "w") as f:
                f.write(body)
            return nvrd.load_config(p)

    def test_bad_mode_rejected(self):
        with self.assertRaises(SystemExit):
            self._load("proxy:\n  mode: nope\n")

    def test_bad_port_rejected(self):
        with self.assertRaises(SystemExit):
            self._load("proxy:\n  rtsp_port: 99999\n")

    def test_same_ports_rejected(self):
        with self.assertRaises(SystemExit):
            self._load("proxy:\n  rtsp_port: 1984\n  api_port: 1984\n")

    def test_default_mode_is_proxy(self):
        cfg = nvrd.load_config(None)
        self.assertEqual(cfg["proxy"]["mode"], "proxy")
        self.assertEqual(cfg["proxy"]["bind"], "127.0.0.1")


class ResuperviseTests(unittest.TestCase):
    """Verify _resupervise() re-resolves each cam (against its device_url, NOT
    its possibly-stale rtsp_url) and pushes the fresh URLs into the supervisor.
    This is the fix for #6: a go2rtc death after a cam IP change must result
    in a fresh config, not a restart with the same dead URL."""

    def test_resupervise_swaps_in_fresh_url(self):
        with _MockLeavesPath(["onvif-rtsp"]) as mp:
            mp.set_stdout("onvif-rtsp", "rtsp://NEWHOST/main\n")
            cfg = nvrd.load_config(None)
            cfg["auth"]["timeout"] = 5
            orch = nvrd.Orchestrator(cfg)
            cam = nvrd.Camera(
                name="cam-old", device_url="http://1.2.3.4/onvif",
                user="", password="", inject_credentials=False,
                rtsp_url="rtsp://127.0.0.1:8554/cam-old",  # local proxy URL
            )
            sup = nvrd.Go2rtcSupervisor([cam], cfg)
            # Avoid actually starting/stopping go2rtc; stub the lifecycle.
            sup.start = lambda: None
            sup.stop = lambda: None
            sup.wait_ready = lambda: True
            orch._resupervise(sup, [cam])
            # Supervisor's cam list now carries the upstream URL, not the
            # 127.0.0.1 proxy one.
            self.assertEqual(len(sup.cams), 1)
            self.assertEqual(sup.cams[0].rtsp_url, "rtsp://NEWHOST/main")
            self.assertEqual(sup.cams[0].name, "cam-old")  # name unchanged

    def test_resupervise_preserves_old_on_resolution_failure(self):
        with _MockLeavesPath(["onvif-rtsp"]) as mp:
            mp.set_rc("onvif-rtsp", 3)  # auth failure
            cfg = nvrd.load_config(None)
            orch = nvrd.Orchestrator(cfg)
            cam = nvrd.Camera(
                name="cam-old", device_url="http://1.2.3.4/onvif",
                user="", password="", inject_credentials=False,
                rtsp_url="rtsp://127.0.0.1:8554/cam-old",
            )
            previous = nvrd.Camera(
                name="cam-old", device_url="http://1.2.3.4/onvif",
                user="", password="", inject_credentials=False,
                rtsp_url="rtsp://1.2.3.4/preserved",
            )
            sup = nvrd.Go2rtcSupervisor([previous], cfg)
            sup.start = lambda: None
            sup.stop = lambda: None
            sup.wait_ready = lambda: True
            orch._resupervise(sup, [cam])
            # Falls back to whatever the supervisor was using before.
            self.assertEqual(sup.cams[0].rtsp_url, "rtsp://1.2.3.4/preserved")


class GatherCamerasTests(unittest.TestCase):
    def _orch(self, cfg_overrides):
        cfg = nvrd.load_config(None)
        nvrd._deep_merge(cfg, cfg_overrides)
        return nvrd.Orchestrator(cfg)

    def test_static_mode_uses_explicit_list(self):
        orch = self._orch({
            "discovery": {"mode": "static", "cameras": [
                {"name": "front", "device_url": "http://1.1.1.1/x"},
                {"name": "back",  "device_url": "http://2.2.2.2/y",
                 "user": "guest", "password": "g"},
            ]},
            "auth": {"user": "admin", "password": "admin",
                     "inject_credentials": True, "timeout": 10},
        })
        cams = orch.gather_cameras()
        self.assertEqual([c.name for c in cams], ["front", "back"])
        # Per-cam override wins
        self.assertEqual(cams[1].user, "guest")
        # Default falls through
        self.assertEqual(cams[0].user, "admin")

    def test_auto_mode_calls_discovery_and_applies_overrides(self):
        with _MockLeavesPath(["onvif-discover"]) as mp:
            mp.set_stdout("onvif-discover",
                          "http://1.1.1.1/x\nhttp://2.2.2.2/y\n")
            orch = self._orch({
                "discovery": {"mode": "auto", "timeout": 2, "cameras": [
                    {"name": "labeled", "device_url": "http://1.1.1.1/x"},
                ]},
            })
            cams = orch.gather_cameras()
            self.assertEqual(len(cams), 2)
            self.assertEqual(cams[0].name, "labeled")
            # The other one gets a derived name.
            self.assertEqual(cams[1].name, "cam-2-2-2-2")


# ---------------------------------------------------------------------------
# CLI meta
# ---------------------------------------------------------------------------

def _run_cli(args, env=None, timeout=15):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True,
        env=env, timeout=timeout, stdin=subprocess.DEVNULL,
    )


class CliMetaTests(unittest.TestCase):
    def test_version(self):
        r = _run_cli(["-V"])
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), f"{nvrd.PROG} {nvrd.VERSION}")

    def test_help(self):
        r = _run_cli(["-h"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("orchestrator", r.stdout)


class CliCheckTests(unittest.TestCase):
    def test_check_direct_mode_with_mocks_succeeds(self):
        # proxy.mode=direct so we don't require go2rtc in PATH for this test.
        with tempfile.TemporaryDirectory() as d, \
             _MockLeavesPath(list(nvrd.LEAF_SCRIPTS)) as mp:
            mp.set_stdout("onvif-discover", "http://1.1.1.1/x\n")
            mp.set_stdout("onvif-rtsp", "rtsp://anon/s\n")

            cfg_path = os.path.join(d, "c.yaml")
            with open(cfg_path, "w") as f:
                f.write(f"""
storage:
  base_dir: {d}
discovery:
  mode: auto
  timeout: 2
proxy:
  mode: direct
auth:
  user: ""
  password: ""
""")
            env = dict(os.environ); env["PATH"] = mp.dir + os.pathsep + env["PATH"]
            r = _run_cli(["-c", cfg_path, "--check"], env=env)
            self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)
            self.assertIn("check OK", r.stdout)
            self.assertIn("proxy.mode=direct", r.stdout)

    def test_check_proxy_mode_without_go2rtc_errors(self):
        # proxy mode (default) must complain when go2rtc is missing.
        with tempfile.TemporaryDirectory() as d, \
             _MockLeavesPath(list(nvrd.LEAF_SCRIPTS)) as mp:
            mp.set_stdout("onvif-discover", "http://1.1.1.1/x\n")
            mp.set_stdout("onvif-rtsp", "rtsp://anon/s\n")

            cfg_path = os.path.join(d, "c.yaml")
            with open(cfg_path, "w") as f:
                # Leave proxy.mode at default ("proxy")
                f.write(f"""
storage:
  base_dir: {d}
discovery:
  mode: auto
  timeout: 2
auth:
  user: ""
  password: ""
""")
            # PATH only has the mock leaves; no go2rtc anywhere.
            env = {"PATH": mp.dir}
            if "SystemRoot" in os.environ:
                env["SystemRoot"] = os.environ["SystemRoot"]
            r = _run_cli(["-c", cfg_path, "--check"], env=env)
            self.assertEqual(r.returncode, 1)
            self.assertIn("go2rtc not in PATH", r.stdout + r.stderr)

    def test_check_proxy_mode_with_go2rtc_mock_succeeds(self):
        # With a fake go2rtc in PATH, --check (which doesn't actually start
        # go2rtc) succeeds.
        with tempfile.TemporaryDirectory() as d, \
             _MockLeavesPath(list(nvrd.LEAF_SCRIPTS) + ["go2rtc"]) as mp:
            mp.set_stdout("onvif-discover", "http://1.1.1.1/x\n")
            mp.set_stdout("onvif-rtsp", "rtsp://anon/s\n")
            cfg_path = os.path.join(d, "c.yaml")
            with open(cfg_path, "w") as f:
                f.write(f"""
storage:
  base_dir: {d}
discovery:
  mode: auto
  timeout: 2
auth:
  user: ""
  password: ""
""")
            env = dict(os.environ); env["PATH"] = mp.dir + os.pathsep + env["PATH"]
            r = _run_cli(["-c", cfg_path, "--check"], env=env)
            self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)
            self.assertIn("proxy.mode=proxy", r.stdout)


if __name__ == "__main__":
    unittest.main()
