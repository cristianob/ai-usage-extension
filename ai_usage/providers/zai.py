"""Z.ai / Zhipu GLM Coding Plan quota provider."""

import json
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
    UsageMetrics,
    WindowPeriod,
)

# Z.ai quota "limits" entries describe their window as (unit, number).
# unit 3 == hours, unit 6 == weeks.
UNIT_HOUR = 3
UNIT_WEEK = 6

TOKENS_LIMIT = "TOKENS_LIMIT"
TIME_LIMIT = "TIME_LIMIT"

# Field names that hold a key in the config files we scan
KEY_FIELD_NAMES = (
    "apiKey",
    "api_key",
    "accessToken",
    "access_token",
    "authToken",
    "auth_token",
    "token",
    "key",
)

# Config keys/sections that identify a Z.ai (global) or Zhipu (CN) credential
ZAI_HINTS = ("zai", "z-ai", "z.ai", "zcode", "glm", "coding-plan", "codingplan")
ZHIPU_HINTS = ("zhipu", "bigmodel")

# Values that are stored encrypted (ZCode wraps its tokens in Electron's
# safeStorage) are useless to us - sending one would just earn a 401.
ENCRYPTED_PREFIXES = ("enc:", "v10", "v11", "encrypted:")

MIN_KEY_LENGTH = 20
MAX_KEY_LENGTH = 400


def _config_home() -> Path:
    """XDG config directory."""
    if xdg := os.environ.get("XDG_CONFIG_HOME"):
        return Path(xdg)
    return Path.home() / ".config"


def _data_home() -> Path:
    """XDG data directory."""
    if xdg := os.environ.get("XDG_DATA_HOME"):
        return Path(xdg)
    return Path.home() / ".local" / "share"


class ZaiCredentialStore:
    """
    Discover a Z.ai key from the tools that already hold one.

    Mirrors what the Claude and Codex providers do with their own CLIs: instead
    of asking the user to paste a key, read the credential another client
    already stored. Probed in order:

    1. ZCode        - ~/.zcode/v2/{credentials,config}.json, ~/.config/zcode/*
    2. OpenCode     - ~/.local/share/opencode/auth.json (zai-coding-plan, ...)
    3. Claude Code  - ~/.claude/settings*.json when ANTHROPIC_BASE_URL points at
                      api.z.ai / open.bigmodel.cn (the GLM-in-Claude-Code setup)

    ZCode keeps its OAuth tokens encrypted in ``credentials.json``
    (``enc:v1:...``, Electron safeStorage) - those are rejected rather than sent
    to the API - but the coding-plan API key sits in plaintext in
    ``config.json`` under ``provider."builtin:zai-coding-plan".options.apiKey``,
    which is what gets picked up.

    A config file lists several provider entries, so candidates are scored:
    ``apiKey`` fields beat other fields, coding-plan paths beat the rest, and
    session JWTs lose to real API keys.

    Returns (key, platform) where platform is "zai" or "zhipu".
    """

    def candidate_files(self) -> list[Path]:
        """Config files that may hold a Z.ai credential, most specific first."""
        config = _config_home()
        home = Path.home()
        return [
            home / ".zcode" / "v2" / "credentials.json",
            home / ".zcode" / "v2" / "config.json",
            home / ".zcode" / "credentials.json",
            home / ".zcode" / "config.json",
            home / ".zcode" / "auth.json",
            config / "zcode" / "config.json",
            config / "zcode" / "auth.json",
            _data_home() / "opencode" / "auth.json",
            config / "opencode" / "auth.json",
            home / ".claude" / "settings.local.json",
            home / ".claude" / "settings.json",
        ]

    def load(self) -> tuple[str, str] | None:
        """Find a key and the platform it belongs to, or None."""
        for path in self.candidate_files():
            data = self._read_json(path)
            if data is None:
                continue

            if path.parts[-2] == ".claude":
                found = self._from_claude_settings(data)
            else:
                found = self._best_candidate(data)

            if found:
                return found

        return None

    def _best_candidate(self, data: Any) -> tuple[str, str] | None:
        """Pick the most credible key in one config file."""
        candidates = list(self._collect(data, ""))
        if not candidates:
            return None
        score, key, platform = max(candidates, key=lambda item: item[0])
        return key, platform

    def _collect(self, node: Any, path_hint: str):
        """
        Yield (score, key, platform) for every plausible credential in a tree.

        A config file holds several provider entries - inactive plans, API-key
        entries, session JWTs - so candidates are scored instead of taking the
        first match in dict order.
        """
        if isinstance(node, dict):
            for key, value in node.items():
                child_path = f"{path_hint}.{key}"

                if isinstance(value, str):
                    platform = self._platform_for(child_path)
                    if platform is None or not self._is_usable_key(value):
                        continue

                    score = 0
                    if key in ("apiKey", "api_key"):
                        score += 100
                    elif key in KEY_FIELD_NAMES:
                        score += 50
                    else:
                        # A bare string under a provider id, e.g.
                        # {"zai-coding-plan": "sk-..."}
                        score += 20

                    if "coding-plan" in child_path.lower():
                        score += 20
                    # Session JWTs work against the chat endpoints, not the
                    # quota API, so prefer a real API key when both exist.
                    if value.startswith("eyJ"):
                        score -= 40

                    yield score, value.strip(), platform
                else:
                    yield from self._collect(value, child_path)

        elif isinstance(node, list):
            for index, item in enumerate(node):
                yield from self._collect(item, f"{path_hint}.{index}")

    def _read_json(self, path: Path) -> Any:
        """Parse a JSON file, or None when missing/unreadable/invalid."""
        try:
            if not path.is_file():
                return None
            return json.loads(path.read_text())
        except (OSError, PermissionError, ValueError):
            return None

    def _from_claude_settings(self, data: Any) -> tuple[str, str] | None:
        """
        Read a GLM-in-Claude-Code setup.

        Only trust the token when the base URL actually points at Z.ai, so we
        never send an Anthropic token to Z.ai's servers.
        """
        if not isinstance(data, dict):
            return None
        env = data.get("env")
        if not isinstance(env, dict):
            return None

        base_url = str(env.get("ANTHROPIC_BASE_URL") or "").lower()
        platform = self._platform_for(base_url)
        if platform is None:
            return None

        for field in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
            token = env.get(field)
            if isinstance(token, str) and token.strip():
                return token.strip(), platform

        return None

    def _is_usable_key(self, value: Any) -> bool:
        """
        Whether a discovered string can actually be used as a key.

        Rejects encrypted blobs (ZCode's ``enc:v1:...``) and anything that does
        not look like a bearer credential, so we never authenticate with junk.
        """
        if not isinstance(value, str):
            return False

        candidate = value.strip()
        if not (MIN_KEY_LENGTH <= len(candidate) <= MAX_KEY_LENGTH):
            return False

        lowered = candidate.lower()
        if any(lowered.startswith(prefix) for prefix in ENCRYPTED_PREFIXES):
            return False
        # Config files are full of endpoints, paths and model ids
        if "://" in lowered or lowered.startswith(("http", "/", "~", "file:")):
            return False
        # Status strings and enum values ("oauth_provider_inactive")
        if not any(char.isdigit() for char in candidate):
            return False

        # Keys are opaque tokens: no whitespace, no JSON-ish punctuation.
        return all(char.isalnum() or char in "._-+/=" for char in candidate)

    def _platform_for(self, text: str) -> str | None:
        """Map a config path or URL onto a platform, or None when unrelated."""
        lowered = text.lower()
        if any(hint in lowered for hint in ZHIPU_HINTS):
            return "zhipu"
        if any(hint in lowered for hint in ZAI_HINTS):
            return "zai"
        return None


class ZaiProvider(BaseProvider):
    """
    Provider for the Z.ai GLM Coding Plan quota endpoint.

    Environment Variables:
        ZAI_API_KEY: Z.ai API key (global platform, api.z.ai)
        ZHIPU_API_KEY: Zhipu API key (CN platform, open.bigmodel.cn)
        ZAI_PLATFORM: Force platform, "zai" (default) or "zhipu"

    Endpoint (undocumented, used by the official coding-plan dashboards):
        GET /api/monitor/usage/quota/limit
        Authorization: <key>      (no "Bearer" prefix)

    The response exposes a rolling 5 hour token window and a weekly token
    window, both as percentage used, plus the next reset timestamp in
    milliseconds. Those two windows are normalized into ``five_hour`` /
    ``seven_day`` entries in ``raw`` so consumers can treat Z.ai exactly like
    Claude.
    """

    name = ProviderName.ZAI

    HOSTS = {
        "zai": "https://api.z.ai",
        "zhipu": "https://open.bigmodel.cn",
    }
    QUOTA_PATH = "/api/monitor/usage/quota/limit"
    TOKEN_ENV_VAR = "ZAI_API_KEY"

    def __init__(self, api_key: str | None = None, platform: str | None = None) -> None:
        """
        Initialize the Z.ai provider.

        Args:
            api_key: API key. If not provided, reads from config (env var or env
                file), then from ZCode / OpenCode / Claude Code credentials.
            platform: "zai" or "zhipu". Defaults to $ZAI_PLATFORM, else the
                platform of whichever credential was discovered.
        """
        discovered_platform = None

        if api_key:
            self._api_key: str | None = api_key
        else:
            from ai_usage.config import config

            self._api_key = config.get_token(ProviderName.ZAI)

            if not self._api_key:
                found = ZaiCredentialStore().load()
                if found:
                    self._api_key, discovered_platform = found

        resolved = platform or self._forced_platform() or discovered_platform \
            or self._detect_platform()
        self._platform = str(resolved).lower()
        if self._platform not in self.HOSTS:
            self._platform = "zai"

    def _forced_platform(self) -> str | None:
        """Platform explicitly pinned through ZAI_PLATFORM."""
        if forced := os.environ.get("ZAI_PLATFORM"):
            return forced
        from ai_usage.config import load_env_file

        return load_env_file().get("ZAI_PLATFORM")

    def _detect_platform(self) -> str:
        """Infer the platform from which key variable is set."""
        from ai_usage.config import load_env_file

        env_file = load_env_file()
        if not (os.environ.get("ZAI_API_KEY") or env_file.get("ZAI_API_KEY")):
            if os.environ.get("ZHIPU_API_KEY") or env_file.get("ZHIPU_API_KEY"):
                return "zhipu"
        return "zai"

    @property
    def quota_url(self) -> str:
        """Full quota endpoint URL for the active platform."""
        return f"{self.HOSTS[self._platform]}{self.QUOTA_PATH}"

    def is_configured(self) -> bool:
        """Check if an API key is available."""
        return bool(self._api_key)

    def get_config_help(self) -> str:
        """Get configuration instructions."""
        return f"""Z.ai GLM Coding Plan Configuration:

Credentials are discovered automatically from, in order:
  1. ${self.TOKEN_ENV_VAR} (env var or ~/.config/ai-usage/env)
  2. ZCode        - ~/.config/zcode/config.json
  3. OpenCode     - ~/.local/share/opencode/auth.json
  4. Claude Code  - ~/.claude/settings.json when ANTHROPIC_BASE_URL is a Z.ai host

To set a key by hand:
   export {self.TOKEN_ENV_VAR}=...
   (create one at https://z.ai/manage-apikey/apikey-list)

For the CN platform (open.bigmodel.cn) use ZHIPU_API_KEY instead,
or set ZAI_PLATFORM=zhipu.
"""

    async def fetch(self, window: WindowPeriod = WindowPeriod.DAY_7) -> ProviderResult:
        """Fetch Z.ai coding plan quota."""
        if not self.is_configured():
            return self._make_error_result(
                window=window,
                error=f"Not configured. Set {self.TOKEN_ENV_VAR} environment variable.",
            )

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    self.quota_url,
                    headers={
                        # Z.ai's monitor API expects the raw key, no "Bearer".
                        "Authorization": self._api_key or "",
                        "Accept": "application/json",
                        "Accept-Language": "en-US,en",
                        "Content-Type": "application/json",
                        "User-Agent": "ai-usage",
                    },
                )

                if response.status_code in (401, 403):
                    raise AuthenticationError("Invalid Z.ai API key")

                if response.status_code == 429:
                    return self._make_error_result(
                        window=window,
                        error="Rate limited. Try again later.",
                        raw={"status_code": 429},
                    )

                if response.status_code != 200:
                    return self._make_error_result(
                        window=window,
                        error=f"API error: HTTP {response.status_code}",
                        raw={"status_code": response.status_code, "body": response.text},
                    )

                return self._parse_response(response.json(), window)

        except AuthenticationError:
            raise
        except httpx.TimeoutException:
            return self._make_error_result(window=window, error="Request timed out")
        except httpx.RequestError as e:
            return self._make_error_result(window=window, error=f"Network error: {e}")
        except Exception as e:
            raise ProviderError(f"Unexpected error: {e}") from e

    def _parse_response(self, payload: dict, window: WindowPeriod) -> ProviderResult:
        """
        Parse the quota response into normalized metrics.

        Expected structure:
        {
            "code": 200,
            "data": {
                "level": "lite",
                "limits": [
                    {"type": "TOKENS_LIMIT", "unit": 3, "number": 5,
                     "percentage": 16.0, "currentValue": 1234, "total": 40000000,
                     "nextResetTime": 1786361842000},
                    {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, ...},
                    {"type": "TIME_LIMIT", "percentage": 2.0, ...}
                ]
            }
        }
        """
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        limits = data.get("limits") or []

        # The endpoint answers 200 with an in-body error code, e.g. code 500
        # "当前用户不存在coding plan" when the account has no coding plan. The raw
        # message is kept in ``raw`` only - callers get an English one.
        code = payload.get("code")
        if not limits:
            message = str(payload.get("msg") or "").strip()
            if code is not None and str(code) not in ("0", "200"):
                if "coding plan" in message.lower():
                    message = "No active coding plan for this key"
                return self._make_error_result(
                    window=window,
                    error=message or f"API error: code {code}",
                    raw=payload,
                )
            return self._make_error_result(
                window=window,
                error=message or "No quota windows returned",
                raw=payload,
            )

        five_hour: dict[str, Any] | None = None
        seven_day: dict[str, Any] | None = None
        mcp: dict[str, Any] | None = None

        for item in limits:
            if not isinstance(item, dict):
                continue

            if item.get("type") == TIME_LIMIT:
                mcp = self._normalize_window(item)
                continue

            if item.get("type") != TOKENS_LIMIT:
                continue

            unit = self._as_number(item.get("unit"))
            number = self._as_number(item.get("number"))
            normalized = self._normalize_window(item)

            if unit == UNIT_HOUR and number == 5:
                five_hour = normalized
            elif unit == UNIT_WEEK and number == 1:
                seven_day = normalized
            elif five_hour is None and unit == UNIT_HOUR:
                five_hour = normalized
            elif seven_day is None and unit == UNIT_WEEK:
                seven_day = normalized

        selected = five_hour if window == WindowPeriod.HOUR_5 else seven_day
        selected = selected or seven_day or five_hour or {}

        utilization = selected.get("utilization")
        remaining = None if utilization is None else 100.0 - float(utilization)

        reset_at = None
        if resets_at := selected.get("resets_at"):
            try:
                reset_at = datetime.fromisoformat(resets_at)
            except (ValueError, TypeError):
                pass

        metrics = UsageMetrics(
            remaining=remaining,
            limit=100.0,  # Percentage-based quota
            reset_at=reset_at,
            input_tokens=selected.get("used_tokens"),
            cost=None,
            requests=None,
            output_tokens=None,
        )

        raw: dict[str, Any] = dict(payload)
        raw["five_hour"] = five_hour
        raw["seven_day"] = seven_day
        raw["mcp_month"] = mcp
        raw["plan_level"] = data.get("level")
        raw["platform"] = self._platform

        return ProviderResult(
            provider=self.name,
            window=window,
            metrics=metrics,
            raw=raw,
        )

    def _normalize_window(self, item: dict) -> dict[str, Any]:
        """Convert one Z.ai limit entry to the shared window shape."""
        percentage = self._as_number(item.get("percentage"))
        used = self._as_number(item.get("currentValue"))
        total = self._as_number(item.get("total"))

        return {
            "utilization": None if percentage is None else float(percentage),
            "resets_at": self._epoch_ms_to_iso(item.get("nextResetTime")),
            "used_tokens": None if used is None else int(used),
            "total_tokens": None if total is None else int(total),
        }

    def _epoch_ms_to_iso(self, value: Any) -> str | None:
        """Convert a millisecond epoch to an ISO 8601 UTC string."""
        number = self._as_number(value)
        if number is None or number <= 0:
            return None
        try:
            return datetime.fromtimestamp(number / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None

    def _as_number(self, value: Any) -> float | None:
        """Coerce a value to float, or None when not numeric."""
        if isinstance(value, bool) or value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number == number else None  # reject NaN
