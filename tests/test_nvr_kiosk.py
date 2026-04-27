"""Offline tests for nvr-kiosk.

We exercise:
  * argv builder (kiosk-tuned mpv flags)
  * stream selection against a fake go2rtc HTTP server
  * timeout / error paths

The real script ends in `os.execv(mpv, ...)`; we stop short of execing in
tests by relying on `--print-url` (a mode that returns the resolved URL
on stdout instead of execing).
"""

import http.server
import importlib.util
import socket
import subprocess
import sys
import threading
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "nvr-kiosk"


def _load_module():
    loader = SourceFileLoader("nvr_kiosk_mod", str(SCRIPT))
    spec = importlib.util.spec_from_loader("nvr_kiosk_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nvr_kiosk_mod"] = mod
    loader.exec_module(mod)
    return mod


nk = _load_module()


# ---------------------------------------------------------------------------
# Fake go2rtc HTTP server
# ---------------------------------------------------------------------------

class _FakeGo2rtc:
    """Tiny HTTP server returning a configurable JSON body at /api/streams."""

    def __init__(self, body=b'{}', status=200):
        self.body = body
        self.status = status
        self._srv = None
        self._thread = None
        self.port = None

    def __enter__(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/api/streams":
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, *_a, **_k):
                pass

        self._srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return self

    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *_):
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(timeout=2)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------

class BuildMpvArgvTests(unittest.TestCase):
    def test_kiosk_defaults(self):
        argv = nk.build_mpv_argv("/usr/bin/mpv", "rtsp://127.0.0.1:8554/cam",
                                 fullscreen=True, loop=True, no_audio=True,
                                 transport="tcp", verbose=False)
        self.assertEqual(argv[0], "/usr/bin/mpv")
        # Low-latency profile and rtsp_transport must be present.
        self.assertIn("--profile=low-latency", argv)
        self.assertIn("--demuxer-lavf-o-add=rtsp_transport=tcp", argv)
        self.assertIn("--fullscreen", argv)
        self.assertIn("--loop-file=inf", argv)
        self.assertIn("--no-audio", argv)
        self.assertIn("--really-quiet", argv)
        # The RTSP URL must be the very last arg.
        self.assertEqual(argv[-1], "rtsp://127.0.0.1:8554/cam")

    def test_no_loop_no_fullscreen_no_mute(self):
        argv = nk.build_mpv_argv("/usr/bin/mpv", "rtsp://x/y",
                                 fullscreen=False, loop=False, no_audio=False,
                                 transport="udp", verbose=True)
        self.assertNotIn("--fullscreen", argv)
        self.assertNotIn("--loop-file=inf", argv)
        self.assertNotIn("--no-audio", argv)
        self.assertNotIn("--really-quiet", argv)
        self.assertIn("--demuxer-lavf-o-add=rtsp_transport=udp", argv)


class FetchStreamsTests(unittest.TestCase):
    def test_object_with_streams(self):
        with _FakeGo2rtc(body=b'{"camA": {}, "camB": {}}') as g:
            self.assertEqual(sorted(nk.fetch_streams(g.url())), ["camA", "camB"])

    def test_empty(self):
        with _FakeGo2rtc(body=b'{}') as g:
            self.assertEqual(nk.fetch_streams(g.url()), [])

    def test_non_200(self):
        with _FakeGo2rtc(status=503, body=b'oops') as g:
            with self.assertRaises(OSError):
                nk.fetch_streams(g.url())

    def test_invalid_json(self):
        with _FakeGo2rtc(body=b'{not json') as g:
            with self.assertRaises(OSError):
                nk.fetch_streams(g.url())

    def test_https_rejected(self):
        with self.assertRaises(ValueError):
            nk.fetch_streams("https://127.0.0.1/")

    def test_unreachable(self):
        # Free port that nothing is listening on.
        with self.assertRaises(OSError):
            nk.fetch_streams(f"http://127.0.0.1:{_free_port()}", http_timeout=0.5)


class WaitForStreamTests(unittest.TestCase):
    def test_picks_first_when_no_name(self):
        with _FakeGo2rtc(body=b'{"camA": {}}') as g:
            self.assertEqual(
                nk.wait_for_stream(g.url(), None, total_timeout=2.0,
                                   poll_interval=0.05, verbose=False),
                "camA",
            )

    def test_picks_named(self):
        with _FakeGo2rtc(body=b'{"camA": {}, "camB": {}}') as g:
            self.assertEqual(
                nk.wait_for_stream(g.url(), "camB", total_timeout=2.0,
                                   poll_interval=0.05, verbose=False),
                "camB",
            )

    def test_times_out_when_no_streams(self):
        with _FakeGo2rtc(body=b'{}') as g:
            with self.assertRaises(RuntimeError) as cm:
                nk.wait_for_stream(g.url(), None, total_timeout=0.3,
                                   poll_interval=0.05, verbose=False)
            self.assertIn("no streams", str(cm.exception))

    def test_times_out_when_named_stream_absent(self):
        with _FakeGo2rtc(body=b'{"other": {}}') as g:
            with self.assertRaises(RuntimeError) as cm:
                nk.wait_for_stream(g.url(), "want", total_timeout=0.3,
                                   poll_interval=0.05, verbose=False)
            self.assertIn("want", str(cm.exception))

    def test_unreachable_raises_oserror(self):
        url = f"http://127.0.0.1:{_free_port()}"
        with self.assertRaises(OSError):
            nk.wait_for_stream(url, None, total_timeout=0.3,
                               poll_interval=0.05, verbose=False)


# ---------------------------------------------------------------------------
# CLI tests via subprocess (use --print-url to avoid actually exec'ing mpv)
# ---------------------------------------------------------------------------

def _run(argv, env=None, timeout=10):
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + argv,
        capture_output=True, text=True, timeout=timeout, env=env,
    )


class CliPrintUrlTests(unittest.TestCase):
    def test_prints_resolved_url(self):
        with _FakeGo2rtc(body=b'{"cam-1-2-3-4": {}}') as g:
            r = _run(["--api-url", g.url(),
                      "--rtsp-base", "rtsp://127.0.0.1:8554",
                      "--wait-timeout", "2", "--poll-interval", "0.1",
                      "--print-url"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertEqual(r.stdout.strip(), "rtsp://127.0.0.1:8554/cam-1-2-3-4")

    def test_default_rtsp_base_uses_api_host(self):
        with _FakeGo2rtc(body=b'{"only": {}}') as g:
            r = _run(["--api-url", g.url(),
                      "--wait-timeout", "2", "--poll-interval", "0.1",
                      "--print-url"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertEqual(r.stdout.strip(), "rtsp://127.0.0.1:8554/only")

    def test_network_error_exits_2(self):
        url = f"http://127.0.0.1:{_free_port()}"
        r = _run(["--api-url", url, "--wait-timeout", "0.3",
                  "--poll-interval", "0.05", "--print-url"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("unreachable", r.stderr)

    def test_protocol_error_exits_4(self):
        with _FakeGo2rtc(body=b'{}') as g:
            r = _run(["--api-url", g.url(), "--wait-timeout", "0.3",
                      "--poll-interval", "0.05", "--print-url"])
            self.assertEqual(r.returncode, 4)
            self.assertIn("no streams", r.stderr)


class MetaTests(unittest.TestCase):
    def test_version(self):
        r = _run(["-V"])
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.strip().startswith("nvr-kiosk "))

    def test_help(self):
        r = _run(["-h"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("go2rtc", r.stdout)


if __name__ == "__main__":
    unittest.main()
