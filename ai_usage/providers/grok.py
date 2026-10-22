"""Grok Build (the `grok` CLI) subscription usage, as shown by its `/usage` command."""

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ai_usage.file_lock import cache_dir, file_lock
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

# The CLI asks its chat proxy, which forwards to the backend GetGrokCreditsConfig.
BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
TOKEN_AUTH_HEADER = "xai-grok-cli"
CLIENT_VERSION = "1.0.45"

# Renewal runs grok itself; a failed attempt is not repeated for a while.
RENEW_TIMEOUT_SECONDS = 60.0
RENEW_COOLDOWN_SECONDS = 600.0
# Treat a token this close to expiry as expired, like the CLI's early invalidation.
EXPIRY_MARGIN = timedelta(seconds=60)


@dataclass(frozen=True)
class GrokCredentials:
    """The grok.com session `grok login` stored."""

    key: str
    user_id: str
    expires_at: datetime | None

    def is_fresh(self) -> bool:
        return self.expires_at is None or self.expires_at - EXPIRY_MARGIN > datetime.now(
            timezone.utc
        )


def _auth_path() -> Path:
    if path := os.environ.get("GROK_AUTH_PATH"):
        return Path(path)
    home = os.environ.get("GROK_HOME")
    return (Path(home) if home else Path.home() / ".grok") / "auth.json"


def _is_xai_issuer(issuer: Any) -> bool:
    host = urlparse(issuer).hostname if isinstance(issuer, str) else None
    return host is not None and (host == "x.ai" or host.endswith(".x.ai"))


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class GrokCredentialStore:
    """
    Read-only view of the grok CLI's auth.json.

    It never refreshes the token itself: grok's refresh tokens are single-use,
    so spending one here would sign the CLI out. Renewal is left to grok (see
    GrokProvider._renew_session).
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _auth_path()

    def load(self) -> GrokCredentials | None:
        try:
            store = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(store, dict):
            return None

        # One entry per login scope; only a first-party grok.com login can read
        # billing (plain API keys cannot). The newest login wins.
        candidates = []
        for entry in store.values():
            if not isinstance(entry, dict):
                continue
            if entry.get("auth_mode") not in ("oidc", "external"):
                continue
            if not _is_xai_issuer(entry.get("oidc_issuer")):
                continue
            if not entry.get("key") or not entry.get("user_id"):
                continue
            candidates.append(entry)
        if not candidates:
            return None

        epoch = datetime.min.replace(tzinfo=timezone.utc)
        best = max(candidates, key=lambda e: _parse_time(e.get("create_time")) or epoch)
        return GrokCredentials(
            key=str(best["key"]),
            user_id=str(best["user_id"]),
            expires_at=_parse_time(best.get("expires_at")),
        )


def _cents(value: Any) -> float | None:
    """proto3 JSON drops zero scalars, so a $0 Cent arrives as {}."""
    if not isinstance(value, dict):
        return None
    try:
        return float(value.get("val", 0))
    except (TypeError, ValueError):
        return None


def _window_label(period: dict[str, Any], start: datetime | None, end: datetime | None) -> str:
    period_type = str(period.get("type") or "")
    if "WEEKLY" in period_type:
        return "7d"
    if "MONTHLY" in period_type:
        return "30d"
    if start and end:
        days = round((end - start).total_seconds() / 86400)
        if days >= 1:
            return f"{days}d"
    return "usage"


def parse_credits(data: dict[str, Any]) -> dict[str, Any]:
    """
    Read the allowance meter the way the CLI does.

    `creditUsagePercent` is preferred; older servers only send the deprecated
    `used` / `monthlyLimit` pair. proto3 omits a 0 percentage, so a config with
    a current period but no percentage means nothing has been used yet.
    """
    config = data.get("config")
    if not isinstance(config, dict):
        raise ValueError("Grok billing response has no config.")

    period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
    start = _parse_time(period.get("start") or config.get("billingPeriodStart"))
    end = _parse_time(period.get("end") or config.get("billingPeriodEnd"))

    percent = config.get("creditUsagePercent")
    if not isinstance(percent, (int, float)) or isinstance(percent, bool):
        limit, used = _cents(config.get("monthlyLimit")), _cents(config.get("used"))
        if limit and limit > 0:
            percent = (used or 0.0) / limit * 100
        elif period:
            percent = 0.0
        else:
            raise ValueError("Grok billing response has no usage meter.")

    prepaid = _cents(config.get("prepaidBalance"))
    return {
        "label": _window_label(period, start, end),
        "utilization": float(percent),
        "resets_at": end,
        "prepaid_usd": prepaid / 100 if prepaid else None,
    }


class GrokProvider(BaseProvider):
    """
    Provider for the Grok Build subscription allowance (SuperGrok / X Premium+).

    Reuses the session from `grok login`; there is nothing to configure here.
    The session lasts hours, so when it has expired the provider runs
    `grok models` - which lists models without spending usage - and lets grok
    renew its own session under its own lock before reading it again.
    """

    name = ProviderName.GROK

    def __init__(
        self,
        credentials: GrokCredentials | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        store: GrokCredentialStore | None = None,
        grok_command: list[str] | None = None,
        state_dir: Path | None = None,
    ) -> None:
        self._store = store or GrokCredentialStore()
        self._credentials = credentials or self._store.load()
        self._transport = transport
        self._grok_command = grok_command
        self._state_dir = state_dir or cache_dir()

    def is_configured(self) -> bool:
        return self._credentials is not None

    def get_config_help(self) -> str:
        return """Grok Build Provider Configuration:

1. Install the Grok CLI: https://x.ai/cli
2. Run: grok login   (sign in with your grok.com account)

The session in ~/.grok/auth.json is reused; set GROK_HOME or GROK_AUTH_PATH
if it lives elsewhere. API-key logins cannot read the subscription meter."""

    async def fetch(self, window: WindowPeriod = WindowPeriod.DAY_7) -> ProviderResult:
        creds = self._credentials
        if creds is None:
            return self._make_error_result(window, "Not configured. Run `grok login`.")
        renewed = False
        if not creds.is_fresh():
            creds, renewed = await self._renew_session(), True

        while True:
            if creds is None:
                raise AuthenticationError("Grok session expired. Run `grok` once to renew it.")
            try:
                response = await self._get_billing(creds)
            except httpx.TimeoutException:
                return self._make_error_result(window, "Grok request timed out.")
            except httpx.RequestError as exc:
                return self._make_error_result(window, f"Grok network error: {exc}")
            if response.status_code in (401, 403) and not renewed:
                # Revoked before its expiry: one renewal, then one retry.
                creds, renewed = await self._renew_session(), True
                continue
            break

        if response.status_code in (401, 403):
            raise AuthenticationError("Grok rejected the session. Run `grok login` again.")
        if response.status_code == 429:
            raise RateLimitError("Grok rate limited. Try again later.")
        if response.status_code != 200:
            return self._make_error_result(window, f"Grok billing error: HTTP {response.status_code}")

        try:
            meter = parse_credits(response.json())
        except ValueError as exc:
            return self._make_error_result(window, str(exc))
        except Exception as exc:
            raise ProviderError(f"Unexpected Grok response: {exc}") from exc

        resets_at = meter["resets_at"]
        raw: dict[str, Any] = {
            "windows": [
                {
                    "label": meter["label"],
                    "utilization": meter["utilization"],
                    "resets_at": resets_at.isoformat() if resets_at else None,
                }
            ]
        }
        if meter["prepaid_usd"] is not None:
            raw["prepaid_usd"] = meter["prepaid_usd"]

        return ProviderResult(
            provider=self.name,
            window=window,
            metrics=UsageMetrics(
                remaining=100.0 - meter["utilization"],
                limit=100.0,
                reset_at=resets_at,
            ),
            raw=raw,
        )

    async def _get_billing(self, creds: GrokCredentials) -> httpx.Response:
        async with httpx.AsyncClient(timeout=15.0, transport=self._transport) as client:
            return await client.get(
                BILLING_URL,
                headers={
                    "Authorization": f"Bearer {creds.key}",
                    "X-XAI-Token-Auth": TOKEN_AUTH_HEADER,
                    "x-userid": creds.user_id,
                    "x-grok-client-version": CLIENT_VERSION,
                    "x-grok-client-mode": "headless",
                    "Accept": "application/json",
                },
            )

    def _grok_argv(self) -> list[str] | None:
        if self._grok_command:
            return self._grok_command
        local = Path.home() / ".local" / "bin" / "grok"
        grok = shutil.which("grok") or (str(local) if local.exists() else None)
        return [grok, "models"] if grok else None

    async def _renew_session(self) -> GrokCredentials | None:
        """
        Run `grok models` so grok renews its own session, then re-read it.

        Serialized across ai-usage processes: whoever waits on the lock first
        re-reads auth.json and skips the run when someone already renewed it.
        A failed attempt is not repeated within RENEW_COOLDOWN_SECONDS, so an
        unrecoverable session does not spawn grok on every refresh.
        """
        stamp = self._state_dir / "grok_renew.stamp"
        try:
            async with file_lock(self._state_dir / "grok_renew.lock", RENEW_TIMEOUT_SECONDS + 5):
                creds = self._store.load()
                if creds and creds.is_fresh() and creds != self._credentials:
                    return creds
                try:
                    if time.time() - stamp.stat().st_mtime < RENEW_COOLDOWN_SECONDS:
                        return None
                except OSError:
                    pass
                argv = self._grok_argv()
                if argv is None:
                    return None
                stamp.touch()
                await _run_quietly(argv, RENEW_TIMEOUT_SECONDS)
                creds = self._store.load()
                return creds if creds and creds.is_fresh() else None
        except TimeoutError:
            return None


async def _run_quietly(argv: list[str], timeout: float) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
