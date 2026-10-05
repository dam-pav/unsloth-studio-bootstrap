"""Constrain the HTTP probe before it can read credentials or contact a server."""

import argparse
import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "sqlite_concurrency_probe", Path(__file__).with_name("sqlite-concurrency.py")
)
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


class HTTPProbeTests(unittest.TestCase):
    def test_http_and_https_origins(self):
        for url in ("http://127.0.0.1:8000", "https://studio.example", "http://[::1]:8000"):
            with self.subTest(url=url):
                self.assertEqual(PROBE.http_base(url + "/"), url)

    def test_invalid_origins_are_rejected(self):
        for url in (
            "file:///etc/passwd", "ftp://studio.example", "studio.example",
            "http:///missing-host", "https://user:password@studio.example",
            "https://studio.example?token=secret", "https://studio.example#fragment",
            "http://studio.example:bad", "http://studio.example:99999", "http://[broken",
        ):
            with self.subTest(url=url), self.assertRaises(argparse.ArgumentTypeError):
                PROBE.http_base(url)

    def test_redirects_cannot_forward_credentials(self):
        handler = PROBE.NoRedirect()
        for target in ("https://other.example", "file:///etc/passwd", "http://127.0.0.1:8000/"):
            with self.subTest(target=target):
                self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, target))


if __name__ == "__main__":
    unittest.main()
