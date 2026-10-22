"""Claude OAuth provider for Claude Code subscription quota."""

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from ai_usage.claude_cli_auth import extract_claude_cli_token
from ai_usage.file_lock import cache_dir, file_lock
from ai_usage.providers.base import (
    AuthenticationError,
    BaseProvider,
    ProviderError,
    ProviderName,
    ProviderResult,
    UsageMetrics,
    WindowPeriod,
)


# The usage endpoint answers 429 to more than one read every 2 minutes per account.
MIN_FETCH_INTERVAL = timedelta(seconds=120)
LOCK_TIMEOUT_SECONDS = 45.0


@dataclass
class _Snapshot:
    """Last Claude usage response, shared by every ai-usage process on the machine."""

    account: str
    next_fetch_at: datetime
    data: dict | None = None
    fetched_at: datetime | None = None


class _SharedUsageCache:
    """
    One file for all processes: the extension, the TUI and every CLI call.

    A file lock serializes them, so while one process is fetching the others wait
    and then read its answer instead of making their own request.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory or cache_dir()
        self._path = self._dir / "claude_usage.json"
        self._lock_path = self._dir / "claude_usage.lock"

    def locked(self):
        return file_lock(self._lock_path, LOCK_TIMEOUT_SECONDS)

    def read(self, account: str) -> _Snapshot | None:
        try:
            raw = json.loads(self._path.read_text())
            snapshot = _Snapshot(
                account=raw["account"],
                next_fetch_at=datetime.fromisoformat(raw["next_fetch_at"]),
                data=raw.get("data"),
                fetched_at=(
                    datetime.fromisoformat(raw["fetched_at"]) if raw.get("fetched_at") else None
                ),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return snapshot if snapshot.account == account else None

    def write(self, snapshot: _Snapshot) -> None:
        payload = {
            "account": snapshot.account,
            "next_fetch_at": snapshot.next_fetch_at.isoformat(),
            "data": snapshot.data,
            "fetched_at": snapshot.fetched_at.isoformat() if snapshot.fetched_at else None,
        }
        tmp = self._path.with_suffix(".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, self._path)
        except OSError:
            pass


def _retry_after(response: httpx.Response) -> timedelta:
    try:
        seconds = float(response.headers.get("retry-after", ""))
    except ValueError:
        return MIN_FETCH_INTERVAL
    return max(MIN_FETCH_INTERVAL, timedelta(seconds=seconds))


class ClaudeOAuthProvider(BaseProvider):
    """
    Provider for Claude Code OAuth usage metrics.

    Uses the OAuth token generated via `claude setup-token` to fetch
    subscription quota information.

    Environment Variables:
        CLAUDE_CODE_OAUTH_TOKEN: OAuth token (sk-ant-oat...)

    Note: This endpoint is unofficial and may change. Code parses defensively.
    """

    name = ProviderName.CLAUDE
    USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
    TOKEN_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"

    def __init__(
        self,
        token: str | None = None,
        cache_dir: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Initialize the Claude OAuth provider.

        Args:
            token: OAuth token. If not provided, reads from environment,
                   then falls back to Claude CLI credentials.
            cache_dir: Where the cross-process usage cache lives.
                       Defaults to ~/.cache/ai-usage/
        """
        self._token = token or os.environ.get(self.TOKEN_ENV_VAR) or extract_claude_cli_token()
        self._cache = _SharedUsageCache(cache_dir)
        self._transport = transport

    def is_configured(self) -> bool:
        """Check if OAuth token is available."""
        return self._token is not None and self._token.startswith("sk-ant-")

    def get_config_help(self) -> str:
        """Get configuration instructions."""
        return f"""Claude OAuth Provider Configuration:

1. Run: claude setup-token
2. Set environment variable:
   export {self.TOKEN_ENV_VAR}=sk-ant-oat01-...

Note: Token must start with 'sk-ant-' prefix."""

    async def fetch(self, window: WindowPeriod = WindowPeriod.DAY_7) -> ProviderResult:
        """
        Fetch Claude Code subscription quota.

        One response carries every window, so it is read at most once per
        MIN_FETCH_INTERVAL and shared, through a file in the cache directory,
        by every window and every process that asks within that interval.
        """
        if not self.is_configured():
            return self._make_error_result(
                window=window,
                error=f"Not configured. Set {self.TOKEN_ENV_VAR} environment variable.",
            )

        account = hashlib.sha256(self._token.encode()).hexdigest()[:16]
        try:
            async with self._cache.locked():
                snapshot = self._cache.read(account)
                now = datetime.now(timezone.utc)
                if snapshot and now < snapshot.next_fetch_at:
                    return self._from_snapshot(snapshot, window)
                return await self._fetch_and_store(account, window)
        except TimeoutError as e:
            if snapshot := self._cache.read(account):
                return self._from_snapshot(snapshot, window)
            return self._make_error_result(window=window, error=str(e))

    def _from_snapshot(self, snapshot: _Snapshot, window: WindowPeriod) -> ProviderResult:
        if snapshot.data is None:
            retry_at = snapshot.next_fetch_at.astimezone().strftime("%H:%M")
            return self._make_error_result(
                window=window,
                error=f"Rate limited. Retrying after {retry_at}.",
                raw={"status_code": 429},
            )
        result = self._parse_response(snapshot.data, window)
        result.updated_at = snapshot.fetched_at.replace(tzinfo=None)
        return result

    async def _fetch_and_store(self, account: str, window: WindowPeriod) -> ProviderResult:
        try:
            async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as client:
                response = await client.get(
                    self.USAGE_URL,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "anthropic-beta": "oauth-2025-04-20",
                        "Accept": "application/json",
                        "User-Agent": "ai-usage",
                    },
                )
        except httpx.TimeoutException:
            return self._make_error_result(window=window, error="Request timed out")
        except httpx.RequestError as e:
            return self._make_error_result(window=window, error=f"Network error: {e}")

        now = datetime.now(timezone.utc)

        if response.status_code == 429:
            # No data: every process reports the rate limit until the backoff ends.
            snapshot = _Snapshot(account=account, next_fetch_at=now + _retry_after(response))
            self._cache.write(snapshot)
            return self._from_snapshot(snapshot, window)

        if response.status_code == 401:
            raise AuthenticationError("Invalid or expired OAuth token")

        if response.status_code == 403:
            error_body = response.text
            if "user:profile" in error_body:
                return self._make_error_result(
                    window=window,
                    error="OAuth scope error. Fix: unset CLAUDE_CODE_OAUTH_TOKEN && claude setup-token",
                    raw={"status_code": 403, "body": error_body},
                )
            return self._make_error_result(
                window=window,
                error=f"API forbidden: HTTP {response.status_code}",
                raw={"status_code": 403, "body": error_body},
            )

        if response.status_code != 200:
            return self._make_error_result(
                window=window,
                error=f"API error: HTTP {response.status_code}",
                raw={"status_code": response.status_code, "body": response.text},
            )

        try:
            data = response.json()
        except ValueError as e:
            raise ProviderError(f"Unexpected error: {e}") from e

        snapshot = _Snapshot(
            account=account, next_fetch_at=now + MIN_FETCH_INTERVAL, data=data, fetched_at=now
        )
        self._cache.write(snapshot)
        return self._from_snapshot(snapshot, window)

    def _parse_response(self, data: dict, window: WindowPeriod) -> ProviderResult:
        """
        Parse the API response defensively.

        Expected response format:
        {
            "five_hour": {"utilization": 61.0, "resets_at": "2026-01-28T07:59:59..."},
            "seven_day": {"utilization": 22.0, "resets_at": "2026-02-03T09:59:59..."},
            "extra_usage": {"is_enabled": false, ...}
        }

        utilization is a percentage (0-100) of quota used.
        """
        # Select the appropriate window based on the requested period
        window_key = "seven_day" if window == WindowPeriod.DAY_7 else "five_hour"
        window_data = data.get(window_key, {})

        if not window_data:
            # Fallback to seven_day if specific window not available
            window_data = data.get("seven_day", {})

        # Parse reset time if available
        reset_at = None
        if resets_at := window_data.get("resets_at"):
            try:
                reset_at = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        # Parse utilization percentage (0-100)
        utilization = window_data.get("utilization")
        remaining_percent = None
        if utilization is not None:
            remaining_percent = 100.0 - utilization

        metrics = UsageMetrics(
            remaining=remaining_percent,  # Store as percentage remaining
            limit=100.0,  # Total quota is 100%
            reset_at=reset_at,
            # Claude doesn't provide these in OAuth endpoint
            cost=None,
            requests=None,
            input_tokens=None,
            output_tokens=None,
        )

        return ProviderResult(
            provider=self.name,
            window=window,
            metrics=metrics,
            raw=data,
        )
