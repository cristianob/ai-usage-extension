"""Contract tests for the Grok Build (grok CLI) provider."""

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from ai_usage.providers import build_providers
from ai_usage.providers.base import AuthenticationError, ProviderName
from ai_usage.providers.grok import GrokCredentials, GrokCredentialStore, GrokProvider

FUTURE = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
PAST = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


def oidc_entry(key: str, created: str, expires: str = FUTURE) -> dict:
    return {
        "key": key,
        "auth_mode": "oidc",
        "create_time": created,
        "user_id": "user-1",
        "oidc_issuer": "https://auth.x.ai",
        "oidc_client_id": "client",
        "refresh_token": "refresh-secret",
        "expires_at": expires,
    }


# Shapes from grok-build's billing.rs tests (proto3 JSON, camelCase, Cents as {"val"}).
CREDITS = {
    "config": {
        "creditUsagePercent": 42.5,
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": "2026-10-20T00:00:00Z",
            "end": "2026-10-27T00:00:00Z",
        },
        "prepaidBalance": {"val": 750},
    }
}
LEGACY = {
    "config": {
        "monthlyLimit": {"val": 2000},
        "used": {"val": 500},
        "billingPeriodStart": "2026-10-01T00:00:00Z",
        "billingPeriodEnd": "2026-11-01T00:00:00Z",
    }
}


class Backend:
    def __init__(self, body=CREDITS, status: int = 200) -> None:
        self.body, self.status, self.requests = body, status, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body, request=request)


class Sandbox:
    """A private auth.json, state dir and fake `grok` per test - never the real ones."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.auth = root / "auth.json"
        self.state = root / "state"
        self.runs = root / "runs"
        self.write_auth("access-token", FUTURE)

    def close(self) -> None:
        self._tmp.cleanup()

    def write_auth(self, key: str, expires: str) -> None:
        self.auth.write_text(json.dumps({"https://auth.x.ai::c": oidc_entry(key, FUTURE, expires)}))

    def grok(self, renews_to: str | None) -> list[str]:
        """Fake `grok models`: logs the run and, like grok, rewrites auth.json."""
        script = (
            "import json, sys\n"
            f"open({str(self.runs)!r}, 'a').write('x')\n"
            f"new = {renews_to!r}\n"
            "if new:\n"
            f"    data = json.load(open({str(self.auth)!r}))\n"
            "    for e in data.values(): e['key'] = new; e['expires_at'] = "
            f"{FUTURE!r}\n"
            f"    json.dump(data, open({str(self.auth)!r}, 'w'))\n"
        )
        return [sys.executable, "-c", script]

    def run_count(self) -> int:
        return len(self.runs.read_text()) if self.runs.exists() else 0

    def provider(self, backend, creds=None, renews_to: str | None = "renewed-token"):
        return GrokProvider(
            creds,
            httpx.MockTransport(backend),
            store=GrokCredentialStore(self.auth),
            grok_command=self.grok(renews_to),
            state_dir=self.state,
        )


def fetch(backend: Backend, expires_at: datetime | None = None):
    sandbox = Sandbox()
    if expires_at:
        sandbox.write_auth("access-token", expires_at.isoformat())
    try:
        creds = GrokCredentials("access-token", "user-1", expires_at)
        return asyncio.run(sandbox.provider(backend, creds, renews_to=None).fetch())
    finally:
        sandbox.close()


class GrokCredentialStoreTests(unittest.TestCase):
    def load(self, store) -> GrokCredentials | None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            path.write_text(json.dumps(store))
            return GrokCredentialStore(path).load()

    def test_newest_grok_com_login_wins_and_api_keys_are_ignored(self) -> None:
        creds = self.load(
            {
                "xai::api_key": {"key": "xai-api", "auth_mode": "api_key", "user_id": "u",
                                 "create_time": "2026-10-21T00:00:00Z"},
                "https://auth.x.ai::old": oidc_entry("old", "2026-09-01T00:00:00Z"),
                "https://auth.x.ai::new": oidc_entry("new", "2026-10-01T00:00:00Z"),
            }
        )
        self.assertEqual(creds.key, "new")
        self.assertEqual(creds.user_id, "user-1")

    def test_third_party_issuer_is_not_a_grok_login(self) -> None:
        entry = oidc_entry("corp", "2026-10-01T00:00:00Z")
        entry["oidc_issuer"] = "https://login.example.com"
        self.assertIsNone(self.load({"corp": entry}))

    def test_missing_file_means_not_configured(self) -> None:
        self.assertIsNone(GrokCredentialStore(Path("/nonexistent/auth.json")).load())


class GrokProviderTests(unittest.TestCase):
    def test_registered(self) -> None:
        self.assertIn(ProviderName.GROK, build_providers())

    def test_weekly_meter(self) -> None:
        backend = Backend()
        result = fetch(backend)
        self.assertIsNone(result.error)
        window = result.raw["windows"][0]
        self.assertEqual(window["label"], "7d")
        self.assertEqual(window["utilization"], 42.5)
        self.assertEqual(window["resets_at"], "2026-10-27T00:00:00+00:00")
        self.assertAlmostEqual(result.metrics.remaining, 57.5)
        self.assertAlmostEqual(result.raw["prepaid_usd"], 7.5)

        request = backend.requests[0]
        self.assertEqual(request.url.path, "/v1/billing")
        self.assertEqual(request.url.params["format"], "credits")
        self.assertEqual(request.headers["authorization"], "Bearer access-token")
        self.assertEqual(request.headers["x-userid"], "user-1")
        self.assertEqual(request.headers["x-xai-token-auth"], "xai-grok-cli")

    def test_legacy_fields_give_a_monthly_meter(self) -> None:
        window = fetch(Backend(LEGACY)).raw["windows"][0]
        self.assertEqual(window["label"], "31d")
        self.assertAlmostEqual(window["utilization"], 25.0)

    def test_omitted_percentage_means_nothing_used(self) -> None:
        body = {"config": {"currentPeriod": {"type": "USAGE_PERIOD_TYPE_MONTHLY"}}}
        window = fetch(Backend(body)).raw["windows"][0]
        self.assertEqual((window["label"], window["utilization"]), ("30d", 0.0))

    def test_unrenewable_expired_session_is_reported_without_a_request(self) -> None:
        backend = Backend()
        with self.assertRaises(AuthenticationError):
            fetch(backend, expires_at=datetime.fromisoformat(PAST))
        self.assertEqual(backend.requests, [])

    def test_rejected_session(self) -> None:
        with self.assertRaises(AuthenticationError):
            fetch(Backend(status=401))

    def test_response_without_config_is_an_error(self) -> None:
        self.assertIn("no config", fetch(Backend({})).error)


class GrokRenewalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.sandbox.write_auth("expired-token", PAST)

    def tearDown(self) -> None:
        self.sandbox.close()

    def test_expired_session_is_renewed_by_grok_then_read(self) -> None:
        backend = Backend()
        result = asyncio.run(self.sandbox.provider(backend).fetch())
        self.assertIsNone(result.error)
        self.assertEqual(self.sandbox.run_count(), 1)
        self.assertEqual(
            [r.headers["authorization"] for r in backend.requests], ["Bearer renewed-token"]
        )

    def test_failed_renewal_is_not_retried_within_the_cooldown(self) -> None:
        for _ in range(3):
            with self.assertRaises(AuthenticationError):
                asyncio.run(self.sandbox.provider(Backend(), renews_to=None).fetch())
        self.assertEqual(self.sandbox.run_count(), 1)

    def test_concurrent_fetches_run_grok_once(self) -> None:
        async def many():
            providers = [self.sandbox.provider(Backend()) for _ in range(4)]
            return await asyncio.gather(*(p.fetch() for p in providers))

        results = asyncio.run(many())
        self.assertTrue(all(r.error is None for r in results))
        self.assertEqual(self.sandbox.run_count(), 1)

    def test_token_rejected_early_is_renewed_once_and_retried(self) -> None:
        self.sandbox.write_auth("revoked-token", FUTURE)

        class RejectsOld(Backend):
            def __call__(self, request):
                if request.headers["authorization"] == "Bearer revoked-token":
                    self.requests.append(request)
                    return httpx.Response(401, request=request)
                return super().__call__(request)

        backend = RejectsOld()
        result = asyncio.run(self.sandbox.provider(backend).fetch())
        self.assertIsNone(result.error)
        self.assertEqual(len(backend.requests), 2)
        self.assertEqual(self.sandbox.run_count(), 1)


if __name__ == "__main__":
    unittest.main()
