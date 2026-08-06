"""Kimi Code (Moonshot AI) coding plan quota provider."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 needs the backport
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        # Only the Kimi CLI's config.toml needs a TOML parser; without one the
        # other discovery sources still work, so this must not be fatal.
        tomllib = None  # type: ignore[assignment]

from ai_usage.providers.base import (
    AuthenticationError,
    BaseProvider,
    ProviderError,
    ProviderName,
    ProviderResult,
    UsageMetrics,
    WindowPeriod,
)

# Coding-plan base URL. The open platform (api.moonshot.cn / platform.kimi.com)
# has no usage endpoint and rejects coding-plan keys, so it is never used here.
DEFAULT_BASE_URL = "https://api.kimi.com/coding/v1"

# Hosts that identify a coding-plan credential found in someone else's config
KIMI_HOSTS = ("api.kimi.com", "api.moonshot.ai", "api.moonshot.cn")

# Config sections / provider ids that mean "this is a Kimi credential"
KIMI_HINTS = ("kimi", "moonshot", "kimi-code", "kimicode")

KEY_FIELD_NAMES = ("api_key", "apiKey", "accessToken", "access_token", "token", "key")

ENCRYPTED_PREFIXES = ("enc:", "v10", "v11", "encrypted:")

MIN_KEY_LENGTH = 20
MAX_KEY_LENGTH = 400

# Window length (seconds) -> the name the UI knows it by
WINDOW_NAMES = {
    18000: "5h",
    604800: "7d",
    2592000: "30d",
}

SECONDS_PER_UNIT = {
    "SECOND": 1,
    "MINUTE": 60,
    "HOUR": 3600,
    "DAY": 86400,
    "WEEK": 604800,
    "MONTH": 2592000,
}


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


def _as_number(value: Any) -> float | None:
    """Coerce to float, or None when not numeric."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # reject NaN


class KimiCredentialStore:
    """
    Discover a Kimi Code key from the tools that already hold one.

    Same idea as the Claude, Codex and Z.ai stores. Probed in order:

    1. Kimi Code CLI - ~/.kimi/config.toml (also the ~/.kimi-code and
       ~/.config/ layouts), reading ``[providers.*]`` tables
    2. OpenCode       - ~/.local/share/opencode/auth.json (kimi, moonshot, ...)
    3. Claude Code    - ~/.claude/settings*.json when ANTHROPIC_BASE_URL points
                        at a Kimi host (the Kimi-in-Claude-Code setup)

    A coding-plan key looks like ``sk-kimi-...``; an open-platform ``sk-...`` key
    cannot read the usage endpoint, so entries that advertise the open platform
    lose to entries that do not.

    Returns (api_key, base_url) where base_url may be None, meaning "use the
    default coding-plan URL".
    """

    def toml_files(self) -> list[Path]:
        """Kimi CLI config files, most likely first."""
        config = _config_home()
        home = Path.home()
        return [
            home / ".kimi" / "config.toml",
            home / ".kimi-code" / "config.toml",
            config / "kimi" / "config.toml",
            config / "kimi-code" / "config.toml",
        ]

    def json_files(self) -> list[Path]:
        """Other clients' credential files."""
        config = _config_home()
        home = Path.home()
        return [
            _data_home() / "opencode" / "auth.json",
            config / "opencode" / "auth.json",
            home / ".claude" / "settings.local.json",
            home / ".claude" / "settings.json",
        ]

    def load(self) -> tuple[str, str | None] | None:
        """Find a usable key, or None."""
        for path in self.toml_files():
            found = self._from_toml(path)
            if found:
                return found

        for path in self.json_files():
            data = self._read_json(path)
            if data is None:
                continue

            if path.parts[-2] == ".claude":
                found = self._from_claude_settings(data)
            else:
                found = self._best_json_candidate(data)

            if found:
                return found

        return None

    # ------------------------------------------------------------ TOML ----

    def _from_toml(self, path: Path) -> tuple[str, str | None] | None:
        """
        Read the Kimi CLI's own config.

        Every ``[providers.<name>]`` table carries a type, a base_url and an
        api_key; the best candidate wins so a secondary provider entry (a proxy,
        an open-platform key) cannot shadow the coding-plan one.
        """
        if tomllib is None:
            return None

        try:
            if not path.is_file():
                return None
            data = tomllib.loads(path.read_text())
        except (OSError, PermissionError, ValueError):
            return None

        providers = data.get("providers")
        if not isinstance(providers, dict):
            return None

        candidates: list[tuple[int, str, str | None]] = []

        for name, entry in providers.items():
            if not isinstance(entry, dict):
                continue

            key = entry.get("api_key")
            if not self._is_usable_key(key):
                continue
            key = str(key).strip()

            base_url = entry.get("base_url")
            base_url = str(base_url).strip() if isinstance(base_url, str) else None

            score = self._score(key, base_url, f"{name} {entry.get('type', '')}")
            if score is None:
                continue
            candidates.append((score, key, base_url))

        if not candidates:
            return None

        _, key, base_url = max(candidates, key=lambda item: item[0])
        return key, base_url

    # ------------------------------------------------------------ JSON ----

    def _read_json(self, path: Path) -> Any:
        """Parse a JSON file, or None when missing/unreadable/invalid."""
        try:
            if not path.is_file():
                return None
            return json.loads(path.read_text())
        except (OSError, PermissionError, ValueError):
            return None

    def _from_claude_settings(self, data: Any) -> tuple[str, str | None] | None:
        """
        Read a Kimi-in-Claude-Code setup.

        Only trust the token when the base URL really is a Kimi host, so an
        Anthropic token is never sent to Moonshot.
        """
        if not isinstance(data, dict):
            return None
        env = data.get("env")
        if not isinstance(env, dict):
            return None

        base_url = str(env.get("ANTHROPIC_BASE_URL") or "").lower()
        if not any(host in base_url for host in KIMI_HOSTS):
            return None

        for field in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
            token = env.get(field)
            if self._is_usable_key(token):
                # That base URL speaks the Anthropic protocol, not the usage
                # API, so fall back to the default coding-plan URL.
                return str(token).strip(), None

        return None

    def _best_json_candidate(self, data: Any) -> tuple[str, str | None] | None:
        """Pick the most credible key in a JSON credential file."""
        candidates = list(self._collect(data, ""))
        if not candidates:
            return None
        _, key, base_url = max(candidates, key=lambda item: item[0])
        return key, base_url

    def _collect(self, node: Any, path_hint: str):
        """Yield (score, key, base_url) for plausible credentials in a tree."""
        if isinstance(node, dict):
            for name, value in node.items():
                child_path = f"{path_hint}.{name}"

                if isinstance(value, str):
                    if not self._is_usable_key(value):
                        continue
                    base_url = node.get("base_url") or node.get("baseURL")
                    base_url = base_url if isinstance(base_url, str) else None
                    score = self._score(value, base_url, child_path)
                    if score is None:
                        continue
                    if name not in KEY_FIELD_NAMES:
                        # A bare string under a provider id, e.g. {"kimi": "sk-..."}
                        score -= 30
                    yield score, value.strip(), base_url
                else:
                    yield from self._collect(value, child_path)

        elif isinstance(node, list):
            for index, item in enumerate(node):
                yield from self._collect(item, f"{path_hint}.{index}")

    # ---------------------------------------------------------- scoring ----

    def _score(self, key: str, base_url: str | None, context: str) -> int | None:
        """
        Rank a candidate, or None when it is not Kimi's at all.

        Context is the config path or provider name walked through; a Kimi hint
        has to appear either there, in the base URL, or in the key prefix.
        """
        haystack = f"{context} {base_url or ''}".lower()
        is_kimi = key.startswith("sk-kimi-") or any(h in haystack for h in KIMI_HINTS)
        if not is_kimi:
            return None

        score = 0
        if key.startswith("sk-kimi-"):
            score += 100  # the coding-plan key format
        if base_url and "coding" in base_url.lower():
            score += 40
        if base_url and any(
            host in base_url.lower() for host in ("api.moonshot.cn", "platform.kimi.com")
        ):
            score -= 60  # open platform: no usage endpoint
        if "kimi" in haystack:
            score += 10

        return score

    def _is_usable_key(self, value: Any) -> bool:
        """Whether a discovered string can be used as a bearer credential."""
        if not isinstance(value, str):
            return False

        candidate = value.strip()
        if not (MIN_KEY_LENGTH <= len(candidate) <= MAX_KEY_LENGTH):
            return False

        lowered = candidate.lower()
        if any(lowered.startswith(prefix) for prefix in ENCRYPTED_PREFIXES):
            return False
        if "://" in lowered or lowered.startswith(("http", "/", "~", "file:")):
            return False
        if not any(char.isdigit() for char in candidate):
            return False

        return all(char.isalnum() or char in "._-+/=" for char in candidate)


class KimiProvider(BaseProvider):
    """
    Provider for the Kimi Code (Moonshot AI) coding plan quota.

    Environment Variables:
        KIMI_API_KEY / KIMI_CODING_API_KEY: coding-plan key (sk-kimi-...)
        KIMI_BASE_URL: override the coding-plan base URL

    Endpoint (undocumented, used by the Kimi Code CLI's /usage command):
        GET <base>/usages       (older builds answer on /usage)
        Authorization: Bearer sk-kimi-...

    The response lists one entry per rate-limit window. They are normalized into
    a ``windows`` array in ``raw`` so the UI can render any set of windows the
    plan happens to have.
    """

    name = ProviderName.KIMI

    TOKEN_ENV_VAR = "KIMI_API_KEY"
    USAGE_PATHS = ("/usages", "/usage")

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        """
        Initialize the Kimi provider.

        Args:
            api_key: coding-plan key. If not given, read from config (env var or
                env file), then from the Kimi CLI / OpenCode / Claude Code.
            base_url: coding-plan base URL. Defaults to $KIMI_BASE_URL, the URL
                stored next to a discovered key, then the official one.
        """
        discovered_base_url = None

        if api_key:
            self._api_key: str | None = api_key
        else:
            from ai_usage.config import config

            self._api_key = config.get_token(ProviderName.KIMI)

            if not self._api_key:
                found = KimiCredentialStore().load()
                if found:
                    self._api_key, discovered_base_url = found

        self._base_url = (
            base_url or self._configured_base_url() or discovered_base_url or DEFAULT_BASE_URL
        ).rstrip("/")

    def _configured_base_url(self) -> str | None:
        """Base URL pinned through KIMI_BASE_URL."""
        if url := os.environ.get("KIMI_BASE_URL"):
            return url
        from ai_usage.config import load_env_file

        return load_env_file().get("KIMI_BASE_URL")

    def is_configured(self) -> bool:
        """Check if a key is available."""
        return bool(self._api_key)

    def get_config_help(self) -> str:
        """Get configuration instructions."""
        return f"""Kimi Code Provider Configuration:

Credentials are discovered automatically from, in order:
  1. ${self.TOKEN_ENV_VAR} (env var or ~/.config/ai-usage/env)
  2. Kimi Code CLI - ~/.kimi/config.toml, [providers.*] api_key
  3. OpenCode      - ~/.local/share/opencode/auth.json
  4. Claude Code   - ~/.claude/settings.json when ANTHROPIC_BASE_URL is a Kimi host

To set a key by hand:
   export {self.TOKEN_ENV_VAR}=sk-kimi-...

It must be a Kimi Code console key (sk-kimi-...). An open-platform key
(sk-..., api.moonshot.cn) cannot read the usage endpoint.
"""

    async def fetch(self, window: WindowPeriod = WindowPeriod.DAY_7) -> ProviderResult:
        """Fetch Kimi Code quota."""
        if not self.is_configured():
            return self._make_error_result(
                window=window,
                error=f"Not configured. Set {self.TOKEN_ENV_VAR} environment variable.",
            )

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            # The usage endpoint is the CLI's; other user agents have been seen
            # rejected with 403, so identify the same way it does.
            "User-Agent": "KimiCLI/1.6",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = None

                for path in self.USAGE_PATHS:
                    response = await client.get(f"{self._base_url}{path}", headers=headers)
                    if response.status_code != 404:
                        break

                assert response is not None

                if response.status_code == 401:
                    raise AuthenticationError(
                        "Invalid Kimi key. It must be a Kimi Code key (sk-kimi-...), "
                        "not an open-platform key."
                    )

                if response.status_code == 403:
                    return self._make_error_result(
                        window=window,
                        error="Access denied. Does this key have coding-plan usage access?",
                        raw={"status_code": 403},
                    )

                if response.status_code == 404:
                    return self._make_error_result(
                        window=window,
                        error=f"No usage endpoint at {self._base_url}. Set KIMI_BASE_URL.",
                        raw={"status_code": 404},
                    )

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

    # ---------------------------------------------------------- parsing ----

    def _parse_response(self, payload: Any, window: WindowPeriod) -> ProviderResult:
        """
        Parse the usage response into normalized windows.

        Two shapes are in the wild:

          {"data": [{"model_name": "all", "limit": 1000, "used": 120,
                     "resetTime": "..."}, ...]}

          {"usage": {...},
           "limits": [{"window": {"duration": 5, "timeUnit": "HOUR"},
                       "detail": {"limit": 1000, "used": 120, "reset_in": 900}}]}
        """
        if not isinstance(payload, dict):
            return self._make_error_result(
                window=window, error="Unexpected response shape", raw={"body": str(payload)[:200]}
            )

        windows = self._extract_windows(payload)

        if not windows:
            message = str(payload.get("message") or payload.get("error") or "").strip()
            return self._make_error_result(
                window=window,
                error=message or "No quota windows returned",
                raw=payload,
            )

        wanted = {
            WindowPeriod.HOUR_5: "5h",
            WindowPeriod.DAY_7: "7d",
            WindowPeriod.DAY_30: "30d",
        }[window]

        selected = next((w for w in windows if w["label"] == wanted), windows[0])

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
            limit=100.0,  # percentage-based, like the other quota providers
            reset_at=reset_at,
            input_tokens=selected.get("used_tokens"),
            cost=None,
            requests=None,
            output_tokens=None,
        )

        raw = dict(payload)
        raw["windows"] = windows
        raw["base_url"] = self._base_url
        # Keep the familiar names too, so anything keyed on them still works.
        for entry in windows:
            if entry["label"] == "5h":
                raw["five_hour"] = entry
            elif entry["label"] == "7d":
                raw["seven_day"] = entry
            elif entry["label"] == "30d":
                raw["thirty_day"] = entry

        return ProviderResult(
            provider=self.name,
            window=window,
            metrics=metrics,
            raw=raw,
        )

    def _extract_windows(self, payload: dict) -> list[dict[str, Any]]:
        """Normalize either payload shape into a list of window dicts."""
        windows: list[dict[str, Any]] = []

        entries = payload.get("data")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                label = self._window_label(entry, entry.get("window"))
                normalized = self._normalize_window(entry, label)
                if normalized:
                    windows.append(normalized)

        limits = payload.get("limits")
        if isinstance(limits, list):
            for entry in limits:
                if not isinstance(entry, dict):
                    continue
                detail = entry.get("detail") if isinstance(entry.get("detail"), dict) else entry
                label = self._window_label(detail, entry.get("window"))
                normalized = self._normalize_window(detail, label)
                if normalized:
                    windows.append(normalized)

        if not windows:
            usage = payload.get("usage")
            if isinstance(usage, dict):
                normalized = self._normalize_window(usage, "7d")
                if normalized:
                    windows.append(normalized)

        # 5h before 7d before 30d, unknown labels last
        order = {"5h": 0, "7d": 1, "30d": 2}
        windows.sort(key=lambda w: order.get(w["label"], 9))

        # Drop duplicates, keeping the first (best-ordered) of each label
        seen: set[str] = set()
        unique = []
        for entry in windows:
            if entry["label"] in seen:
                continue
            seen.add(entry["label"])
            unique.append(entry)

        return unique

    def _window_label(self, entry: dict, window: Any) -> str:
        """Name a window from its declared duration, falling back to its title."""
        if isinstance(window, dict):
            duration = _as_number(window.get("duration"))
            unit = str(window.get("timeUnit") or window.get("time_unit") or "").upper()
            for unit_name, seconds in SECONDS_PER_UNIT.items():
                if unit_name in unit and duration:
                    total = int(duration * seconds)
                    if total in WINDOW_NAMES:
                        return WINDOW_NAMES[total]
                    if total % 86400 == 0:
                        return f"{total // 86400}d"
                    if total % 3600 == 0:
                        return f"{total // 3600}h"
                    return f"{max(1, total // 60)}m"

        # The "all" pseudo-model is the plan-wide (weekly) bucket
        if entry.get("model_name") == "all":
            return "7d"

        title = str(entry.get("name") or entry.get("title") or "").lower()
        if "week" in title:
            return "7d"
        if "month" in title:
            return "30d"
        if "5" in title and "hour" in title:
            return "5h"

        return "5h"

    def _normalize_window(self, entry: dict, label: str) -> dict[str, Any] | None:
        """Convert one usage entry to the shared window shape."""
        limit = _as_number(entry.get("limit") or entry.get("limit_amount"))
        used = _as_number(entry.get("used") if entry.get("used") is not None else entry.get("used_amount"))

        if used is None:
            remaining = _as_number(entry.get("remaining"))
            if remaining is not None and limit is not None:
                used = limit - remaining

        if used is None and limit is None:
            return None

        utilization = None
        if limit and limit > 0 and used is not None:
            utilization = max(0.0, min(100.0, (used / limit) * 100))

        return {
            "label": label,
            "utilization": utilization,
            "resets_at": self._reset_iso(entry),
            "used_tokens": None if used is None else int(used),
            "total_tokens": None if limit is None else int(limit),
        }

    def _reset_iso(self, entry: dict) -> str | None:
        """Read whichever reset field this build uses, as an ISO 8601 string."""
        raw_reset = (
            entry.get("resetTime")
            or entry.get("reset_at")
            or entry.get("reset_time")
        )

        if raw_reset is not None:
            number = _as_number(raw_reset)
            if number is not None and number > 0:
                # Milliseconds if it is far past the year 3000 in seconds
                if number > 1e11:
                    number /= 1000
                try:
                    return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
                except (OverflowError, OSError, ValueError):
                    return None
            if isinstance(raw_reset, str):
                try:
                    parsed = datetime.fromisoformat(raw_reset.replace("Z", "+00:00"))
                except ValueError:
                    return None
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.isoformat()

        reset_in = _as_number(entry.get("reset_in"))
        if reset_in is not None and reset_in > 0:
            stamp = datetime.now(timezone.utc).timestamp() + reset_in
            return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()

        return None
