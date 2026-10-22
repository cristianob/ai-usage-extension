"""Contract tests for the OpenCode Zen and Go providers."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from ai_usage.cache import ResultCache
from ai_usage.providers import build_providers
from ai_usage.providers.base import AuthenticationError, ProviderName, WindowPeriod
from ai_usage.providers.opencode_go import (
    OpenCodeGoCredentialStore,
    OpenCodeGoProvider,
)
from ai_usage.providers.opencode_zen import (
    OpenCodeZenCredentials,
    OpenCodeZenProvider,
    parse_zen_billing,
)


class OpenCodeRegistryTests(unittest.TestCase):
    def test_registry_contains_both_products(self) -> None:
        providers = build_providers()
        self.assertIn(ProviderName.OPENCODE_ZEN, providers)
        self.assertIn(ProviderName.OPENCODE_GO, providers)

    def test_go_auth_json_prefers_product_specific_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "opencode": {"type": "api", "key": "fallback"},
                        "opencode-go": {"type": "api", "key": "preferred"},
                        "unrelated": {"type": "api", "key": "wrong"},
                    }
                )
            )
            with patch.dict(os.environ, {"OPENCODE_API_KEY": ""}, clear=False):
                with patch("ai_usage.config.ENV_FILE_PATH", Path(directory) / "missing"):
                    key = OpenCodeGoCredentialStore([auth_path]).load()
            self.assertEqual(key, "preferred")

    def test_go_auth_json_invalid_encoding_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            auth_path.write_bytes(b"\xff\xfe")
            with patch.dict(os.environ, {"OPENCODE_API_KEY": ""}, clear=False):
                with patch("ai_usage.config.ENV_FILE_PATH", Path(directory) / "missing"):
                    key = OpenCodeGoCredentialStore([auth_path]).load()
            self.assertIsNone(key)


class OpenCodeGoTests(unittest.TestCase):
    PAYLOAD = {
        "usage": {
            "rolling": {
                "status": "ok",
                "percent": 12.5,
                "resetsAt": "2026-08-13T15:00:00.000Z",
            },
            "weekly": {
                "status": "ok",
                "percent": 48,
                "resetsAt": "2026-08-19T15:00:00+00:00",
            },
            "monthly": {
                "status": "ok",
                "percent": 101.5,
                "resetsAt": "2026-09-01T00:00:00Z",
            },
        }
    }

    def test_current_official_schema_maps_all_three_windows(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["authorization"] = request.headers.get("authorization")
            return httpx.Response(200, json=self.PAYLOAD, request=request)

        provider = OpenCodeGoProvider(
            "secret", transport=httpx.MockTransport(handler)
        )
        result = asyncio.run(provider.fetch(WindowPeriod.DAY_7))

        self.assertFalse(result.is_error)
        self.assertEqual(seen["url"], "https://opencode.ai/zen/go/v1/usage")
        self.assertEqual(seen["authorization"], "Bearer secret")
        self.assertEqual([w["label"] for w in result.raw["windows"]], ["5h", "7d", "30d"])
        self.assertEqual(result.raw["windows"][2]["utilization"], 101.5)
        self.assertEqual(result.metrics.usage_percent, 48)
        self.assertNotIn("secret", json.dumps(result.model_dump(mode="json")))

        monthly = provider._parse_response(self.PAYLOAD, WindowPeriod.DAY_30)
        self.assertAlmostEqual(monthly.metrics.usage_percent or 0, 101.5)

    def test_partial_window_success_does_not_become_false_zero(self) -> None:
        payload = {
            "usage": {
                "rolling": self.PAYLOAD["usage"]["rolling"],
                "weekly": {"status": "ok", "percent": True, "resetsAt": "bad"},
            }
        }
        provider = OpenCodeGoProvider("secret")
        result = provider._parse_response(payload, WindowPeriod.DAY_7)

        self.assertFalse(result.is_error)
        self.assertEqual(len(result.raw["windows"]), 1)
        self.assertIsNone(result.metrics.usage_percent)
        self.assertEqual(result.raw["warnings"], ["weekly: invalid window"])

    def test_403_reports_missing_entitlement_not_bad_auth(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(403, request=request))
        result = asyncio.run(OpenCodeGoProvider("secret", transport).fetch())
        self.assertEqual(result.error, "OpenCode Go subscription required.")

    def test_401_is_authentication_error(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(401, request=request))
        with self.assertRaises(AuthenticationError):
            asyncio.run(OpenCodeGoProvider("secret", transport).fetch())


class OpenCodeZenTests(unittest.TestCase):
    HTML = """
    <html><script>
      const decoy={monthlyUsage:999,balance:999};
      const billing={customerID:"cus_123",balance:2500000000,
        monthlyUsage:125000000,monthlyLimit:50,paymentMethodID:"pm_private",last4:"4242"};
    </script></html>
    """

    def test_parser_returns_only_converted_financial_allowlist(self) -> None:
        billing = parse_zen_billing(self.HTML)
        self.assertEqual(
            billing,
            {
                "balance_usd": 25.0,
                "monthly_usage_usd": 1.25,
                "monthly_limit_usd": 50.0,
            },
        )

    def test_parser_accepts_quoted_reordered_fields(self) -> None:
        html = (
            '<script>window.data={"monthlyLimit":null,"monthlyUsage":50000000,'
            '"customerID":"cus_quoted","balance":100000000}</script>'
        )
        self.assertEqual(
            parse_zen_billing(html),
            {
                "balance_usd": 1.0,
                "monthly_usage_usd": 0.5,
                "monthly_limit_usd": None,
            },
        )

    def test_parser_accepts_new_workspace_without_stripe_usage(self) -> None:
        html = (
            '<script>window.data={"balance":100000000,"customerID":null,'
            '"monthlyLimit":null,"monthlyUsage":null}</script>'
        )
        self.assertEqual(
            parse_zen_billing(html),
            {
                "balance_usd": 1.0,
                "monthly_usage_usd": 0.0,
                "monthly_limit_usd": None,
            },
        )

    def test_parser_skips_decoy_and_handles_nested_objects_and_braces(self) -> None:
        html = """
        <script>
          const decoy={customerID:"cus_decoy",note:"not billing"};
          const billing={payment:{kind:"card"},note:"literal { brace }",
            customerID:"cus_real",monthlyUsage:50000000,balance:100000000,
            monthlyLimit:10};
        </script>
        """
        self.assertEqual(parse_zen_billing(html)["monthly_usage_usd"], 0.5)

    def test_parser_ignores_html_apostrophe_before_script(self) -> None:
        html = "<p>What's new</p>" + self.HTML
        self.assertEqual(parse_zen_billing(html)["balance_usd"], 25.0)

    def test_parser_uses_direct_fields_not_nested_decoys(self) -> None:
        html = """
        <script>const billing={nested:{balance:900,monthlyUsage:800},
          customerID:"cus_real",balance:100000000,monthlyUsage:50000000,
          monthlyLimit:10};</script>
        """
        self.assertEqual(
            parse_zen_billing(html),
            {
                "balance_usd": 1.0,
                "monthly_usage_usd": 0.5,
                "monthly_limit_usd": 10.0,
            },
        )

    def test_parser_rejects_nested_customer_anchor(self) -> None:
        html = """
        <script>const unrelated={balance:700000000,monthlyUsage:200000000,
          profile:{customerID:"cus_nested"}};</script>
        """
        with self.assertRaises(ValueError):
            parse_zen_billing(html)

    def test_parser_skips_decoy_with_nested_financial_fields(self) -> None:
        html = """
        <script>
          const decoy={customerID:"cus_decoy",nested:{balance:9,monthlyUsage:8}};
          const billing={customerID:"cus_real",balance:100000000,
            monthlyUsage:50000000,monthlyLimit:null};
        </script>
        """
        self.assertEqual(parse_zen_billing(html)["balance_usd"], 1.0)

    def test_parser_rejects_truncated_billing_object(self) -> None:
        html = '<script>{customerID:"cus_bad",balance:1,monthlyUsage:1'
        with self.assertRaises(ValueError):
            parse_zen_billing(html)

    def test_parser_rejects_missing_monthly_usage(self) -> None:
        html = '<script>window.data={customerID:null,balance:100000000}</script>'
        with self.assertRaises(ValueError):
            parse_zen_billing(html)

    def test_fetch_uses_validated_cookie_and_never_persists_html(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["cookie"] = request.headers.get("cookie")
            return httpx.Response(
                200,
                text=self.HTML,
                headers={"content-type": "text/html; charset=utf-8"},
                request=request,
            )

        credentials = OpenCodeZenCredentials("wrk_ABC123", "cookie-secret")
        provider = OpenCodeZenProvider(credentials, httpx.MockTransport(handler))
        result = asyncio.run(provider.fetch(WindowPeriod.HOUR_5))

        self.assertFalse(result.is_error)
        self.assertEqual(result.window, WindowPeriod.DAY_30)
        self.assertEqual(seen["url"], "https://opencode.ai/workspace/wrk_ABC123/billing")
        self.assertEqual(seen["cookie"], "auth=cookie-secret")
        self.assertEqual(result.metrics.cost, 1.25)
        self.assertEqual(result.metrics.remaining, 48.75)
        serialized = json.dumps(result.model_dump(mode="json"))
        self.assertNotIn("cookie-secret", serialized)
        self.assertNotIn("paymentMethodID", serialized)
        self.assertNotIn("4242", serialized)

    def test_login_redirect_is_authentication_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "/login"}, request=request)

        credentials = OpenCodeZenCredentials("wrk_ABC123", "cookie-secret")
        provider = OpenCodeZenProvider(credentials, httpx.MockTransport(handler))
        with self.assertRaises(AuthenticationError):
            asyncio.run(provider.fetch())

    def test_effective_window_matches_cache_key(self) -> None:
        credentials = OpenCodeZenCredentials("wrk_ABC123", "cookie-secret")
        provider = OpenCodeZenProvider(credentials)
        effective = provider.normalize_window(WindowPeriod.HOUR_5)
        self.assertEqual(effective, WindowPeriod.DAY_30)
        with tempfile.TemporaryDirectory() as directory:
            cache = ResultCache(Path(directory))
            result = provider._make_error_result(effective, "probe")
            result.error = None
            cache.set(result)
            self.assertIsNotNone(cache.get(ProviderName.OPENCODE_ZEN, effective))
            self.assertIsNone(cache.get(ProviderName.OPENCODE_ZEN, WindowPeriod.HOUR_5))


if __name__ == "__main__":
    unittest.main()
