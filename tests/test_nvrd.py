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
    def test_happy_path_with_credentials_and_inject(self):
        with _MockLeavesPath(["onvif-rtsp"]) as mp:
            mp.set_stdout("onvif-rtsp", "rtsp://u:p@1.2.3.4/s\n")
            cam = nvrd.Camera(
                name="cam1",
                device_url="http://1.2.3.4/onvif/device_service",
                user="admin", password="admin",
                inject_credentials=True,
            )
            url = nvrd.resolve_rtsp_url(cam, timeout=5)
            self.assertEqual(url, "rtsp://u:p@1.2.3.4/s")
            args = mp.args("onvif-rtsp")
            self.assertIn("--user", args)
            self.assertIn("admin", args)
            self.assertIn("--inject-credentials", args)
            self.assertEqual(args[-1], "http://1.2.3.4/onvif/device_service")

    def test_no_credentials_no_user_flag(self):
        with _MockLeavesPath(["onvif-rtsp"]) as mp:
            mp.set_stdout("onvif-rtsp", "rtsp://anon/x\n")
            cam = nvrd.Camera(
                name="anon", device_url="http://1.2.3.4/onvif/device_service",
                user="", password="", inject_credentials=False,
            )
            url = nvrd.resolve_rtsp_url(cam, timeout=5)
            self.assertEqual(url, "rtsp://anon/x")
            self.assertNotIn("--user", mp.args("onvif-rtsp"))

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
