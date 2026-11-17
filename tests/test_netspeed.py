"""Тесты netspeed. Запуск из корня проекта:  python -m unittest discover -s tests -v"""
import contextlib
import io
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import netspeed  # noqa: E402

BODY = b"x" * 200_000


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/file.bin":
            self.send_response(200)
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)
        elif self.path == "/nolength.bin":
            self.send_response(200)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(BODY)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/file.bin")
            self.end_headers()
        elif self.path == "/loop":
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.end_headers()
        elif self.path == "/truncated.bin":
            self.send_response(200)
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY[:1000])
            self.wfile.flush()
            self.close_connection = True
        else:
            self.send_error(404)


class ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()


class ConfigTests(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.ini"
            if text is not None:
                path.write_text(text, encoding="utf-8")
            return netspeed.load_config(path)

    def test_missing_file_gives_defaults(self):
        cfg, warnings = self.load(None)
        self.assertEqual(cfg, netspeed.Config())
        self.assertTrue(warnings)

    def test_valid_values(self):
        cfg, warnings = self.load(
            "[settings]\nlogging_enabled = true  # комментарий\nmax_logs = 5\n"
            "download_files = yes\ndownload_first_only = 1\ntimeout = 12.5\n")
        self.assertEqual(warnings, [])
        self.assertEqual(cfg, netspeed.Config(True, 5, True, True, 12.5))

    def test_invalid_values_fall_back_to_defaults(self):
        cfg, warnings = self.load(
            "[settings]\nlogging_enabled = maybe\nmax_logs = -5\n"
            "download_files = 2\ndownload_first_only =\ntimeout = abc\n")
        self.assertEqual(cfg, netspeed.Config())
        self.assertEqual(len(warnings), 5)

    def test_nan_and_zero_timeout_rejected(self):
        for bad in ("nan", "inf", "0", "-1"):
            cfg, _ = self.load(f"[settings]\ntimeout = {bad}\n")
            self.assertEqual(cfg.timeout, 30.0, bad)

    def test_broken_file(self):
        cfg, warnings = self.load("это не ini-файл\n")
        self.assertEqual(cfg, netspeed.Config())
        self.assertTrue(warnings)

    def test_partially_valid(self):
        cfg, warnings = self.load("[settings]\nlogging_enabled = true\nmax_logs = zzz\n")
        self.assertTrue(cfg.logging_enabled)
        self.assertEqual(cfg.max_logs, 0)
        self.assertEqual(len(warnings), 1)


class UrlTests(unittest.TestCase):
    def test_valid(self):
        for raw in ("http://a.com", "https://a.com/x.jpg?q=1", "http://127.0.0.1:8080/f", "https://пример.рф/файл"):
            url, err = netspeed.parse_url(raw)
            self.assertIsNone(err, raw)
            self.assertTrue(url.isascii(), raw)

    def test_invalid(self):
        for raw in ("", "   ", "example.com/x.jpg", "ftp://a.com/x", "http://", "http://a b.com", "http://a.com:99999", "file:///etc/passwd"):
            url, err = netspeed.parse_url(raw)
            self.assertIsNone(url, raw)
            self.assertTrue(err, raw)


class StatsTests(unittest.TestCase):
    def test_summary_uses_only_successful_requests(self):
        R = netspeed.RequestResult
        stats = netspeed.summarize([
            R(1, True, 200, 1_000_000, 1.0),
            R(2, True, 200, 1_000_000, 3.0),
            R(3, False, 503, 500, 0.1, "HTTP 503"),
        ])
        self.assertEqual((stats.ok, stats.failed), (2, 1))
        self.assertAlmostEqual(stats.avg_time, 2.0)
        self.assertEqual(stats.size, 2_000_000)
        self.assertAlmostEqual(stats.speed, 4.0)  # 16 Мбит / 4 с
        self.assertEqual(stats.partial, 500)

    def test_no_successful_requests(self):
        stats = netspeed.summarize([netspeed.RequestResult(1, False, None, 0, 0.1, "x")])
        self.assertIsNone(stats.avg_time)
        self.assertIsNone(stats.speed)
        self.assertEqual(netspeed.fmt_speed(stats.speed), "н/д")


class FetchTests(ServerTestCase):
    def fetch(self, path, **kw):
        return netspeed.fetch(1, self.base + path, 5, lambda *_: None, **kw)

    def test_ok(self):
        r = self.fetch("/file.bin")
        self.assertTrue(r.ok)
        self.assertEqual((r.status, r.size), (200, len(BODY)))

    def test_404(self):
        r = self.fetch("/nope")
        self.assertFalse(r.ok)
        self.assertEqual(r.status, 404)
        self.assertIn("404", r.error)

    def test_no_content_length(self):
        r = self.fetch("/nolength.bin")
        self.assertTrue(r.ok)
        self.assertEqual(r.size, len(BODY))

    def test_redirect_followed_and_loop_stopped(self):
        self.assertTrue(self.fetch("/redirect").ok)
        self.assertFalse(self.fetch("/loop").ok)

    def test_truncated_body_is_error(self):
        r = self.fetch("/truncated.bin")
        self.assertFalse(r.ok)
        self.assertEqual(r.size, 1000)

    def test_connection_refused(self):
        r = netspeed.fetch(1, "http://127.0.0.1:1/x", 5, lambda *_: None)
        self.assertFalse(r.ok)
        self.assertIsNone(r.status)

    def test_save_leaves_no_part_files_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.fetch("/truncated.bin", save_dir=Path(tmp))
            self.assertFalse(r.ok)
            self.assertEqual(list(Path(tmp).iterdir()), [])


class MainTests(ServerTestCase):
    def run_main(self, config_text, argv):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "logs").mkdir()
        (root / "downloads").mkdir()
        (root / "config.ini").write_text(config_text, encoding="utf-8")
        saved = (netspeed.CONFIG_PATH, netspeed.LOGS_DIR, netspeed.DOWNLOADS_DIR, netspeed.BASE_DIR)
        netspeed.CONFIG_PATH, netspeed.LOGS_DIR = root / "config.ini", root / "logs"
        netspeed.DOWNLOADS_DIR, netspeed.BASE_DIR = root / "downloads", root
        self.addCleanup(lambda: (setattr(netspeed, "CONFIG_PATH", saved[0]), setattr(netspeed, "LOGS_DIR", saved[1]),
                                 setattr(netspeed, "DOWNLOADS_DIR", saved[2]), setattr(netspeed, "BASE_DIR", saved[3])))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = netspeed.main(argv)
        return code, out.getvalue(), root

    def test_plain_run(self):
        code, out, root = self.run_main("[settings]\n", [self.base + "/file.bin"])
        self.assertEqual(code, 0)
        self.assertIn("Успешно: 10 из 10", out)
        self.assertIn("Скорость скачивания", out)
        self.assertEqual(list((root / "logs").iterdir()), [])
        self.assertEqual(list((root / "downloads").iterdir()), [])

    def test_unavailable_address(self):
        code, out, _ = self.run_main("[settings]\n", [self.base + "/missing"])
        self.assertEqual(code, 1)
        self.assertIn("недоступен", out)
        self.assertIn("404", out)

    def test_invalid_address(self):
        code, out, _ = self.run_main("[settings]\n", ["not-a-url"])
        self.assertEqual(code, 1)
        self.assertIn("Неверный адрес", out)

    def test_logs_and_download_first_only(self):
        cfg = "[settings]\nlogging_enabled = true\nmax_logs = 3\ndownload_files = true\ndownload_first_only = true\n"
        code, out, root = self.run_main(cfg, [self.base + "/file.bin"])
        self.assertEqual(code, 0)
        runs = list((root / "logs").iterdir())
        self.assertEqual(len(runs), 1)
        log = (runs[0] / "run.log").read_text(encoding="utf-8")
        self.assertEqual(log.count(" OK status=200"), 10)
        self.assertEqual(log.count("saved=no"), 9)
        files = [p for p in (root / "downloads").rglob("*") if p.is_file()]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_size, len(BODY))

    def test_download_all(self):
        code, _, root = self.run_main("[settings]\ndownload_files = true\n", [self.base + "/file.bin"])
        files = [p for p in (root / "downloads").rglob("*") if p.is_file()]
        self.assertEqual(len(files), 10)

    def test_max_logs_zero_keeps_nothing(self):
        _, out, root = self.run_main("[settings]\nlogging_enabled = true\nmax_logs = 0\n", [self.base + "/file.bin"])
        self.assertIn("max_logs = 0", out)
        self.assertEqual(list((root / "logs").iterdir()), [])

    def test_log_rotation(self):
        cfg = "[settings]\nlogging_enabled = true\nmax_logs = 2\n"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        logs = Path(tmp.name)
        for name in ("2020-01-01_00-00-01", "2020-01-01_00-00-02", "2020-01-01_00-00-03"):
            (logs / name).mkdir()
        (logs / ".keep.py").write_text("x")
        saved = netspeed.LOGS_DIR
        netspeed.LOGS_DIR = logs
        self.addCleanup(lambda: setattr(netspeed, "LOGS_DIR", saved))
        netspeed.prune_logs(2)
        left = sorted(p.name for p in logs.iterdir())
        self.assertEqual(left, [".keep.py", "2020-01-01_00-00-02", "2020-01-01_00-00-03"])

    def test_url_list_with_bad_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            lst = Path(tmp) / "urls.txt"
            lst.write_text(f"# комментарий\n{self.base}/file.bin\n\nbad-url\n{self.base}/missing\n", encoding="utf-8")
            code, out, _ = self.run_main("[settings]\n", ["--file", str(lst)])
        self.assertEqual(code, 1)
        self.assertIn("Общая сводка", out)
        self.assertIn("Адресов: 3, полностью успешно: 1", out)

    def test_missing_list_file(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, _, _ = self.run_main("[settings]\n", ["--file", "/no/such/file.txt"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
