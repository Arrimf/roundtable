#!/usr/bin/env python3
"""Лимиты: HTTP-контракт и повторы после сбоя, без сети провайдеров/CLI.

Запуск: python3 RoundTable/test/limits_refresh_test.py
"""
import http.client
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
TMP = tempfile.TemporaryDirectory(prefix="rt-limits-")
os.environ.update(ROUNDTABLE_JOURNAL=TMP.name, CHOIR_RT_NO_DISCOVERY="1",
                  CHOIR_RT_MODELS=str(Path(TMP.name) / "models.json"),
                  CHOIR_RT_VOICES=str(Path(TMP.name) / "voices.json"))
import roundtable as rt  # noqa: E402


class MemorySocket:
    """Настоящие HTTP-парсеры Handler/HTTPResponse, без слушающего порта."""
    def __init__(self, request):
        self.request = request
        self.response = bytearray()

    def makefile(self, *args):
        return io.BytesIO(self.request)

    def sendall(self, data):
        self.response.extend(data)


class LimitsRefreshTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        TMP.cleanup()

    def setUp(self):
        self.release = threading.Event()
        self.calls = 0
        self.result = {"test": {"kind": "ok", "note": "new"}}
        self.old = {"test": {"kind": "ok", "note": "old"}}
        with rt._LIM_LOCK:
            rt._LIM.update(ts=time.time(), data=self.old, busy=False,
                           error=False, retry_at=0.0, waited=False)
            rt._LIM_READY.clear()
        self.patches = [patch.object(rt, "collect_limits", self.collect),
                        patch.object(rt, "VOICES", ["test"]),
                        patch.object(rt, "voice_report", lambda n, lim: dict(name=n, **lim))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        self.release.set()
        self.wait_done()
        for p in reversed(self.patches):
            p.stop()

    def collect(self):
        self.calls += 1
        if not self.release.wait(5):
            raise RuntimeError("test did not release collector")
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def wait_done(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with rt._LIM_LOCK:
                if not rt._LIM["busy"]:
                    return
            time.sleep(.005)
        self.fail("collector did not finish")

    def request(self, method, path):
        body = b"{}" if method == "POST" else b""
        sock = MemorySocket((f"{method} {path} HTTP/1.0\r\n"
                             f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
        rt.Handler(sock, ("127.0.0.1", 12345), None)
        with http.client.HTTPResponse(MemorySocket(bytes(sock.response))) as response:
            response.begin()
            return response.status, json.loads(response.read())

    def voices(self):
        status, data = self.request("GET", "/voices")
        self.assertEqual(status, 200)
        return data

    def test_force_bypasses_ttl_returns_202_and_shares_running_check(self):
        # Свежий кэш не обновляется сам. POST возвращает ответ, пока сбор
        # удерживается Event: проверка неблокирующего HTTP без секундомера.
        self.assertFalse(self.voices()["limits_refreshing"])
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.request("POST", "/limits_refresh"), (202, {"ok": True}))
        self.assertEqual(self.request("POST", "/limits_refresh")[0], 202)
        data = self.voices()
        self.assertTrue(data["limits_refreshing"])
        self.assertFalse(data["limits_error"])
        self.assertEqual(data["voices"][0]["note"], "old")
        self.release.set()
        self.wait_done()
        self.assertEqual(self.calls, 1)
        data = self.voices()
        self.assertFalse(data["limits_refreshing"])
        self.assertFalse(data["limits_error"])
        self.assertEqual(data["voices"][0]["note"], "new")

    def fail_stale_check(self):
        with rt._LIM_LOCK:
            rt._LIM["ts"] = time.time() - rt.LIMITS_TTL - 10
        measured = self.voices()["limits_measured_at"]
        self.result = RuntimeError("expected test failure")
        self.release.set()
        self.wait_done()
        data = self.voices()
        self.assertTrue(data["limits_error"])
        self.assertFalse(data["limits_refreshing"])
        self.assertEqual(data["limits_measured_at"], measured)
        self.assertEqual(data["voices"][0]["note"], "old")
        self.assertEqual(self.calls, 1)
        self.release.clear()
        self.result = {"test": {"kind": "ok", "note": "recovered"}}

    def test_failure_preserves_snapshot_and_manual_retry_bypasses_pause(self):
        self.fail_stale_check()
        self.assertEqual(self.request("POST", "/limits_refresh")[0], 202)
        data = self.voices()
        self.assertTrue(data["limits_refreshing"])
        self.assertFalse(data["limits_error"])
        self.release.set()
        self.wait_done()
        self.assertEqual(self.voices()["voices"][0]["note"], "recovered")

    def test_auto_retry_clears_error_and_button_joins_it(self):
        self.fail_stale_check()
        with rt._LIM_LOCK:
            rt._LIM["retry_at"] = time.time() - 1
        data = self.voices()
        self.assertTrue(data["limits_refreshing"])
        self.assertFalse(data["limits_error"])
        self.assertEqual(self.request("POST", "/limits_refresh")[0], 202)
        self.assertFalse(self.voices()["limits_error"])
        self.release.set()
        self.wait_done()
        self.assertEqual(self.calls, 2)

    def test_only_first_request_waits_even_before_any_success(self):
        with rt._LIM_LOCK:
            rt._LIM.update(ts=0.0, data={})
        with patch.object(rt._LIM_READY, "wait", return_value=False) as wait:
            self.request("POST", "/limits_refresh")
            wait.assert_not_called()
            self.voices()
            wait.assert_called_once_with(rt.FIRST_WAIT)
            self.voices()
            wait.assert_called_once()
            self.result = RuntimeError("expected initial failure")
            self.release.set()
            self.wait_done()
            self.assertTrue(rt._LIM_READY.is_set())
            self.release.clear()
            with rt._LIM_LOCK:
                rt._LIM["retry_at"] = 0.0
            self.assertTrue(self.voices()["limits_refreshing"])
            self.assertTrue(rt._LIM_READY.is_set())
            wait.assert_called_once()


if __name__ == "__main__":
    unittest.main()
