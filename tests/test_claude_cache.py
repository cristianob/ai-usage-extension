"""The Claude usage endpoint is read at most once per 2 minutes, across processes."""

import asyncio
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from ai_usage.providers.base import WindowPeriod
from ai_usage.providers.claude_oauth import ClaudeOAuthProvider

TOKEN = "sk-ant-oat01-test"
USAGE = {
    "five_hour": {"utilization": 40.0, "resets_at": "2026-09-30T20:00:00Z"},
    "seven_day": {"utilization": 10.0, "resets_at": "2026-10-05T20:00:00Z"},
}


class Counter:
    def __init__(self, status: int = 200) -> None:
        self.calls = 0
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.status == 200:
            return httpx.Response(200, json=USAGE, request=request)
        return httpx.Response(self.status, request=request)


def provider(cache_dir: Path, handler: Counter, token: str = TOKEN) -> ClaudeOAuthProvider:
    return ClaudeOAuthProvider(token, cache_dir, httpx.MockTransport(handler))


class ClaudeSharedCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _age_cache(self, seconds: int) -> None:
        path = self.dir / "claude_usage.json"
        raw = json.loads(path.read_text())
        raw["next_fetch_at"] = (
            datetime.fromisoformat(raw["next_fetch_at"]) - timedelta(seconds=seconds)
        ).isoformat()
        path.write_text(json.dumps(raw))

    def test_both_windows_and_new_instances_share_one_request(self) -> None:
        handler = Counter()
        five = asyncio.run(provider(self.dir, handler).fetch(WindowPeriod.HOUR_5))
        seven = asyncio.run(provider(self.dir, handler).fetch(WindowPeriod.DAY_7))
        self.assertEqual(handler.calls, 1)
        self.assertEqual(five.metrics.remaining, 60.0)
        self.assertEqual(seven.metrics.remaining, 90.0)

    def test_concurrent_coroutines_share_one_request(self) -> None:
        handler = Counter()

        async def many():
            return await asyncio.gather(
                *(provider(self.dir, handler).fetch(WindowPeriod.HOUR_5) for _ in range(5))
            )

        results = asyncio.run(many())
        self.assertEqual(handler.calls, 1)
        self.assertTrue(all(r.metrics.remaining == 60.0 for r in results))

    def test_concurrent_processes_share_one_request(self) -> None:
        # Each process counts its own HTTP calls into a file; the total must be 1.
        calls = self.dir / "calls"
        script = textwrap.dedent(f"""
            import asyncio, httpx
            from pathlib import Path
            from ai_usage.providers.claude_oauth import ClaudeOAuthProvider
            def handler(request):
                with open({str(calls)!r}, "a") as f:
                    f.write("x")
                import time; time.sleep(0.3)
                return httpx.Response(200, json={USAGE!r}, request=request)
            p = ClaudeOAuthProvider({TOKEN!r}, Path({str(self.dir)!r}), httpx.MockTransport(handler))
            r = asyncio.run(p.fetch())
            assert r.error is None, r.error
        """)
        procs = [subprocess.Popen([sys.executable, "-c", script]) for _ in range(4)]
        self.assertEqual([p.wait() for p in procs], [0, 0, 0, 0])
        self.assertEqual(calls.read_text(), "x")

    def test_refetches_after_two_minutes(self) -> None:
        handler = Counter()
        asyncio.run(provider(self.dir, handler).fetch())
        self._age_cache(121)
        asyncio.run(provider(self.dir, handler).fetch())
        self.assertEqual(handler.calls, 2)

    def test_rate_limit_is_reported_even_with_older_data_and_backs_off(self) -> None:
        asyncio.run(provider(self.dir, Counter()).fetch())
        self._age_cache(121)

        limited = Counter(status=429)
        first = asyncio.run(provider(self.dir, limited).fetch(WindowPeriod.HOUR_5))
        second = asyncio.run(provider(self.dir, limited).fetch(WindowPeriod.DAY_7))
        self.assertEqual(limited.calls, 1)
        self.assertIn("Rate limited", first.error)
        self.assertIn("Rate limited", second.error)
        self.assertIsNone(second.metrics.remaining)

    def test_backoff_follows_retry_after_when_longer(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"retry-after": "600"}, request=request)

        asyncio.run(ClaudeOAuthProvider(TOKEN, self.dir, httpx.MockTransport(handler)).fetch())
        raw = json.loads((self.dir / "claude_usage.json").read_text())
        wait = datetime.fromisoformat(raw["next_fetch_at"]) - datetime.now(timezone.utc)
        self.assertGreater(wait, timedelta(seconds=590))

    def test_other_account_does_not_reuse_the_cache(self) -> None:
        handler = Counter()
        asyncio.run(provider(self.dir, handler).fetch())
        asyncio.run(provider(self.dir, handler, token="sk-ant-oat01-other").fetch())
        self.assertEqual(handler.calls, 2)

    def test_cache_file_holds_no_token(self) -> None:
        asyncio.run(provider(self.dir, Counter()).fetch())
        self.assertNotIn(TOKEN, (self.dir / "claude_usage.json").read_text())


if __name__ == "__main__":
    unittest.main()
