"""OpenCode Zen pay-as-you-go billing provider."""

import math
import os
import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from ai_usage.providers.base import (
    AuthenticationError,
    BaseProvider,
    ProviderError,
    ProviderName,
    ProviderResult,
    RateLimitError,
    UsageMetrics,
    WindowPeriod,
)

MICROCENTS_PER_USD = 100_000_000
_WORKSPACE_RE = re.compile(r"^wrk_[A-Za-z0-9]+$")
_NUMBER = r"-?\d+(?:\.\d+)?"


@dataclass(frozen=True)
class OpenCodeZenCredentials:
    """Browser session needed by the Zen billing dashboard."""

    workspace_id: str
    auth_cookie: str


def _clean_cookie(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if value.startswith("auth="):
        value = value[5:]
    if not value or any(char in value for char in (";", "\r", "\n", "\0")):
        return None
    return value


def load_zen_credentials() -> OpenCodeZenCredentials | None:
    """Load and validate Zen's workspace id and browser auth cookie."""
    from ai_usage.config import load_env_file

    env_file = load_env_file()
    workspace_id = (
        os.environ.get("OPENCODE_WORKSPACE_ID")
        or env_file.get("OPENCODE_WORKSPACE_ID")
        or ""
    ).strip()
    auth_cookie = _clean_cookie(
        os.environ.get("OPENCODE_AUTH_COOKIE") or env_file.get("OPENCODE_AUTH_COOKIE")
    )
    if not _WORKSPACE_RE.fullmatch(workspace_id) or not auth_cookie:
        return None
    return OpenCodeZenCredentials(workspace_id=workspace_id, auth_cookie=auth_cookie)


def missing_zen_credentials() -> list[str]:
    """Return missing or invalid Zen credential field names without secret values."""
    from ai_usage.config import load_env_file

    env_file = load_env_file()
    workspace_id = (
        os.environ.get("OPENCODE_WORKSPACE_ID")
        or env_file.get("OPENCODE_WORKSPACE_ID")
        or ""
    ).strip()
    cookie = os.environ.get("OPENCODE_AUTH_COOKIE") or env_file.get("OPENCODE_AUTH_COOKIE")
    missing = []
    if not _WORKSPACE_RE.fullmatch(workspace_id):
        missing.append("OPENCODE_WORKSPACE_ID")
    if not _clean_cookie(cookie):
        missing.append("OPENCODE_AUTH_COOKIE")
    return missing


def _extract_billing_object(html: str) -> str | None:
    """Find the smallest complete billing object around a structural anchor."""
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script\s*>", html, re.IGNORECASE | re.DOTALL)
    for script in scripts:
        body = _extract_billing_from_script(script)
        if body is not None:
            return body
    return None


def _extract_billing_from_script(script: str) -> str | None:
    """Find a complete billing object inside one script block."""
    spans: list[tuple[int, int]] = []
    stack: list[int] = []
    quote_char: str | None = None
    escaped = False
    for index, char in enumerate(script):
        if quote_char:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote_char:
                quote_char = None
            continue
        if char in ('"', "'", "`"):
            quote_char = char
        elif char == "{":
            stack.append(index)
        elif char == "}" and stack:
            spans.append((stack.pop(), index + 1))

    anchor_pattern = re.compile(
        r'["\']?customerID["\']?\s*:\s*(?:["\']cus_|null)'
    )
    for anchor in anchor_pattern.finditer(script):
        candidates = sorted(
            (span for span in spans if span[0] <= anchor.start() < span[1]),
            key=lambda span: span[1] - span[0],
        )
        for start, end in candidates:
            body = script[start:end]
            depths = _object_depths(body)
            anchor_offset = anchor.start() - start
            if depths[anchor_offset] != 1:
                continue
            has_balance, _ = _number_field(body, "balance")
            has_monthly_usage, _ = _number_field(body, "monthlyUsage")
            if has_balance and has_monthly_usage:
                return body
    return None


def _number_field(body: str, field: str) -> tuple[bool, float | None]:
    pattern = re.compile(
        rf"(?:^|[,{{])\s*(?P<key>[\"']?{re.escape(field)}[\"']?)\s*:\s*"
        rf"(?P<value>{_NUMBER}|null)\s*(?=[,}}])"
    )
    depths = _object_depths(body)
    for match in pattern.finditer(body):
        if depths[match.start("key")] != 1:
            continue
        raw_value = match.group("value")
        if raw_value == "null":
            return True, None
        value = float(raw_value)
        return True, value if math.isfinite(value) else None
    return False, None


def _object_depths(body: str) -> list[int]:
    """Return object depth at each character while ignoring quoted braces."""
    depths = [0] * len(body)
    depth = 0
    quote_char: str | None = None
    escaped = False
    for index, char in enumerate(body):
        depths[index] = depth
        if quote_char:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote_char:
                quote_char = None
            continue
        if char in ('"', "'", "`"):
            quote_char = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
    return depths


def parse_zen_billing(html: str) -> dict[str, float | None]:
    """Parse only non-sensitive monetary fields from Zen's billing SSR payload."""
    body = _extract_billing_object(html)
    if body is None:
        if any(field in html for field in ("monthlyUsage", "monthlyLimit", "customerID")):
            raise ValueError("OpenCode Zen billing parser is outdated.")
        raise ValueError("OpenCode Zen billing data not found; session may be expired.")

    has_balance, balance = _number_field(body, "balance")
    has_monthly_usage, monthly_usage = _number_field(body, "monthlyUsage")
    _, monthly_limit = _number_field(body, "monthlyLimit")
    if not has_balance or balance is None or not has_monthly_usage:
        raise ValueError("OpenCode Zen billing response is missing required fields.")
    if monthly_usage is None:
        monthly_usage = 0.0

    return {
        "balance_usd": balance / MICROCENTS_PER_USD,
        "monthly_usage_usd": monthly_usage / MICROCENTS_PER_USD,
        "monthly_limit_usd": monthly_limit if monthly_limit and monthly_limit > 0 else None,
    }


class OpenCodeZenProvider(BaseProvider):
    """Provider for OpenCode Zen balance and current monthly spend."""

    name = ProviderName.OPENCODE_ZEN

    def __init__(
        self,
        credentials: OpenCodeZenCredentials | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._credentials = credentials or load_zen_credentials()
        self._transport = transport

    def normalize_window(self, window: WindowPeriod) -> WindowPeriod:
        """Zen's dashboard exposes calendar-month billing only."""
        return WindowPeriod.DAY_30

    def is_configured(self) -> bool:
        """Return whether both Zen browser-session fields are valid."""
        return self._credentials is not None

    def get_config_help(self) -> str:
        """Return Zen browser-session configuration instructions."""
        return """OpenCode Zen Provider Configuration:

1. Open https://opencode.ai and select your workspace.
2. Copy the workspace id from /workspace/wrk_.../billing.
3. Copy the value of the opencode.ai browser cookie named 'auth'.
4. Set:
   export OPENCODE_WORKSPACE_ID=wrk_...
   export OPENCODE_AUTH_COOKIE='Fe26.2**...'

The auth cookie is a session credential. Never commit or share it.
"""

    async def fetch(self, window: WindowPeriod = WindowPeriod.DAY_7) -> ProviderResult:
        """Fetch Zen billing from the authenticated workspace dashboard."""
        window = self.normalize_window(window)
        if not self._credentials:
            return self._make_error_result(
                window,
                "Not configured. Set OPENCODE_WORKSPACE_ID and OPENCODE_AUTH_COOKIE.",
            )

        workspace_id = self._credentials.workspace_id
        url = f"https://opencode.ai/workspace/{quote(workspace_id, safe='')}/billing"
        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.get(
                    url,
                    headers={
                        "Cookie": f"auth={self._credentials.auth_cookie}",
                        "Accept": "text/html",
                        "User-Agent": "ai-usage",
                    },
                )

            if response.status_code in (301, 302, 303, 307, 308, 401, 403):
                raise AuthenticationError("OpenCode Zen session expired or unauthorized.")
            if response.status_code == 429:
                raise RateLimitError("OpenCode Zen rate limited. Try again later.")
            if response.status_code != 200:
                return self._make_error_result(
                    window, f"OpenCode Zen dashboard error: HTTP {response.status_code}"
                )
            if response.url.host != "opencode.ai" or response.url.path != httpx.URL(url).path:
                raise AuthenticationError("OpenCode Zen redirected away from the billing page.")
            content_type = response.headers.get("content-type", "").lower()
            if "text/html" not in content_type:
                return self._make_error_result(
                    window, "OpenCode Zen returned non-HTML billing data."
                )
            body_lower = response.text.lower()
            if "sign in" in body_lower and ("/login" in body_lower or "login" in body_lower):
                raise AuthenticationError("OpenCode Zen session expired or unauthorized.")

            try:
                billing = parse_zen_billing(response.text)
            except ValueError as exc:
                return self._make_error_result(window, str(exc))

            monthly_usage = float(billing["monthly_usage_usd"] or 0.0)
            monthly_limit = billing["monthly_limit_usd"]
            limit = float(monthly_limit) if monthly_limit is not None else None
            remaining = max(0.0, limit - monthly_usage) if limit is not None else None

            return ProviderResult(
                provider=self.name,
                window=window,
                metrics=UsageMetrics(
                    cost=monthly_usage,
                    remaining=remaining,
                    limit=limit,
                ),
                raw={"billing": billing},
            )
        except (AuthenticationError, RateLimitError):
            raise
        except httpx.TimeoutException:
            return self._make_error_result(window, "OpenCode Zen request timed out.")
        except httpx.RequestError as exc:
            return self._make_error_result(window, f"OpenCode Zen network error: {exc}")
        except Exception as exc:
            raise ProviderError(f"Unexpected OpenCode Zen error: {exc}") from exc
