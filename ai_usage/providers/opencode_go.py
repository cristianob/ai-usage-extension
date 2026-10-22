"""OpenCode Go subscription quota provider."""

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


def _data_home() -> Path:
    value = os.environ.get("XDG_DATA_HOME")
    return Path(value) if value else Path.home() / ".local" / "share"


class OpenCodeGoCredentialStore:
    """Find the API key already used by OpenCode Go."""

    def __init__(self, auth_paths: list[Path] | None = None) -> None:
        self._auth_paths = (
            auth_paths
            if auth_paths is not None
            else [
                _data_home() / "opencode" / "auth.json",
                Path.home() / ".config" / "opencode" / "auth.json",
            ]
        )

    def load(self) -> str | None:
        """Load an API key without accepting unrelated credential records."""
        from ai_usage.config import load_env_file

        if key := os.environ.get("OPENCODE_API_KEY"):
            return key.strip() or None
        if key := load_env_file().get("OPENCODE_API_KEY"):
            return key.strip() or None

        for path in self._auth_paths:
            try:
                payload = json.loads(path.read_text())
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue

            # OpenCode uses the same account key for Zen and Go. Prefer the
            # product-specific record; the Zen record is a deliberate fallback.
            for provider_id in ("opencode-go", "opencode"):
                entry = payload.get(provider_id)
                if not isinstance(entry, dict) or entry.get("type") != "api":
                    continue
                key = entry.get("key")
                if isinstance(key, str) and key.strip():
                    return key.strip()
        return None


class OpenCodeGoProvider(BaseProvider):
    """Provider for OpenCode Go rolling, weekly and monthly quota windows."""

    name = ProviderName.OPENCODE_GO
    USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
    TOKEN_ENV_VAR = "OPENCODE_API_KEY"

    _WINDOWS = (
        ("rolling", "5h", WindowPeriod.HOUR_5),
        ("weekly", "7d", WindowPeriod.DAY_7),
        ("monthly", "30d", WindowPeriod.DAY_30),
    )

    def __init__(
        self,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = (
            OpenCodeGoCredentialStore().load()
            if api_key is None
            else (api_key.strip() or None)
        )
        self._transport = transport

    def is_configured(self) -> bool:
        """Return whether an OpenCode API key is available."""
        return bool(self._api_key)

    def get_config_help(self) -> str:
        """Return OpenCode Go configuration instructions."""
        return """OpenCode Go Provider Configuration:

1. Connect OpenCode Go from OpenCode's /connect menu. ai-usage reads:
   ~/.local/share/opencode/auth.json (entry: opencode-go)

2. Or set:
   export OPENCODE_API_KEY=sk-...
"""

    async def fetch(self, window: WindowPeriod = WindowPeriod.DAY_7) -> ProviderResult:
        """Fetch all Go quota windows from OpenCode's official usage API."""
        if not self._api_key:
            return self._make_error_result(
                window,
                "Not configured. Connect OpenCode Go or set OPENCODE_API_KEY.",
            )

        try:
            async with httpx.AsyncClient(timeout=15.0, transport=self._transport) as client:
                response = await client.get(
                    self.USAGE_URL,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Accept": "application/json",
                        "User-Agent": "ai-usage",
                    },
                )

            if response.status_code == 401:
                raise AuthenticationError("Invalid OpenCode API key.")
            if response.status_code == 403:
                return self._make_error_result(window, "OpenCode Go subscription required.")
            if response.status_code == 429:
                raise RateLimitError("OpenCode Go rate limited. Try again later.")
            if response.status_code != 200:
                return self._make_error_result(
                    window, f"OpenCode Go API error: HTTP {response.status_code}"
                )

            try:
                payload = response.json()
            except ValueError:
                return self._make_error_result(window, "OpenCode Go returned invalid JSON.")
            return self._parse_response(payload, window)
        except (AuthenticationError, RateLimitError):
            raise
        except httpx.TimeoutException:
            return self._make_error_result(window, "OpenCode Go request timed out.")
        except httpx.RequestError as exc:
            return self._make_error_result(window, f"OpenCode Go network error: {exc}")
        except Exception as exc:
            raise ProviderError(f"Unexpected OpenCode Go error: {exc}") from exc

    def _parse_response(self, payload: Any, window: WindowPeriod) -> ProviderResult:
        """Normalize the current official Go response without leaking its schema."""
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            return self._make_error_result(window, "OpenCode Go response has no usage data.")

        windows: list[dict[str, Any]] = []
        metrics_by_window: dict[WindowPeriod, UsageMetrics] = {}
        warnings: list[str] = []

        for source_name, label, period in self._WINDOWS:
            entry = usage.get(source_name)
            normalized = self._parse_window(entry)
            if normalized is None:
                if entry is not None:
                    warnings.append(f"{source_name}: invalid window")
                continue
            percent, reset_at, status = normalized
            windows.append(
                {
                    "label": label,
                    "utilization": percent,
                    "resets_at": reset_at.isoformat(),
                    "status": status,
                }
            )
            metrics_by_window[period] = UsageMetrics(
                remaining=100.0 - percent,
                limit=100.0,
                reset_at=reset_at,
            )

        if not windows:
            return self._make_error_result(window, "OpenCode Go returned no valid usage windows.")

        raw: dict[str, Any] = {"windows": windows}
        if warnings:
            raw["warnings"] = warnings

        return ProviderResult(
            provider=self.name,
            window=window,
            metrics=metrics_by_window.get(window, UsageMetrics()),
            raw=raw,
        )

    @staticmethod
    def _parse_window(entry: Any) -> tuple[float, datetime, str] | None:
        if not isinstance(entry, dict):
            return None
        percent = entry.get("percent")
        resets_at = entry.get("resetsAt")
        status = entry.get("status")
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            return None
        percent = float(percent)
        if not math.isfinite(percent) or not isinstance(resets_at, str):
            return None
        try:
            parsed_reset = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed_reset.tzinfo is None:
            parsed_reset = parsed_reset.replace(tzinfo=timezone.utc)
        else:
            parsed_reset = parsed_reset.astimezone(timezone.utc)
        if status not in ("ok", "rate-limited"):
            status = "unknown"
        return percent, parsed_reset, status
