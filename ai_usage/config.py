"""Configuration management for ai-usage."""

import os
from pathlib import Path
from typing import Any

from ai_usage.claude_cli_auth import extract_claude_cli_token
from ai_usage.providers.base import ProviderName

# Env file location
ENV_FILE_PATH = Path.home() / ".config" / "ai-usage" / "env"

# Pre-rename location, still read when the current one is absent
LEGACY_ENV_FILE_PATH = Path.home() / ".config" / "usage-tui" / "env"


def env_file_path() -> Path:
    """Path the env file is read from (new location wins, legacy as fallback)."""
    if ENV_FILE_PATH.exists() or not LEGACY_ENV_FILE_PATH.exists():
        return ENV_FILE_PATH
    return LEGACY_ENV_FILE_PATH


def load_env_file() -> dict[str, str]:
    """Load key=value pairs from env file. Returns empty dict if missing/unreadable."""
    result: dict[str, str] = {}
    path = env_file_path()
    try:
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    result[key.strip()] = value.strip()
    except (OSError, PermissionError):
        pass
    return result


def write_env_file(updates: dict[str, str]) -> None:
    """Write/update keys in env file. Preserves other lines."""
    ENV_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)

    existing_lines: list[str] = []
    existing_keys: set[str] = set()

    source = env_file_path()
    try:
        if source.exists():
            existing_lines = source.read_text().splitlines()
    except (OSError, PermissionError):
        pass

    new_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, _ = stripped.partition("=")
            key = key.strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                existing_keys.add(key)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    # Add new keys not already in file
    for key, value in updates.items():
        if key not in existing_keys:
            new_lines.append(f"{key}={value}")

    ENV_FILE_PATH.write_text("\n".join(new_lines) + "\n")
    ENV_FILE_PATH.chmod(0o600)


class Config:
    """
    Application configuration.

    Currently reads only from environment variables.
    Follows the principle of preferring env vars for security.
    """

    # Environment variable mappings
    ENV_VARS = {
        ProviderName.CLAUDE: "CLAUDE_CODE_OAUTH_TOKEN",
        ProviderName.OPENROUTER: "OPENROUTER_API_KEY",
        ProviderName.CODEX: "CODEX_ACCESS_TOKEN",
        ProviderName.ZAI: "ZAI_API_KEY",
    }

    # Provider descriptions
    PROVIDER_INFO = {
        ProviderName.CLAUDE: {
            "name": "Claude Code",
            "description": "Claude Code subscription quota via OAuth",
            "official": False,
            "note": "Uses unofficial OAuth endpoint with beta header",
        },
        ProviderName.OPENROUTER: {
            "name": "OpenRouter",
            "description": "OpenRouter API credits and usage",
            "official": True,
            "note": "Uses /api/v1/key for credits and limits",
        },
        ProviderName.CODEX: {
            "name": "OpenAI Codex",
            "description": "OpenAI Codex weekly usage via ChatGPT backend",
            "official": False,
            "note": "Reads credentials from ~/.codex/auth.json",
        },
        ProviderName.ZAI: {
            "name": "Z.ai",
            "description": "Z.ai GLM Coding Plan / ZCode 5h + weekly token quota",
            "official": False,
            "note": "Reuses ZCode / OpenCode / Claude Code credentials when present",
        },
    }

    def get_token(self, provider: ProviderName) -> str | None:
        """
        Get authentication token for a provider.

        Priority: environment variable > env file > provider-specific credential stores.
        """
        env_var = self.ENV_VARS.get(provider)

        # 1. Check environment variable (highest priority)
        if env_var and (token := os.environ.get(env_var)):
            return token

        # 2. Check env file
        if env_var:
            env_file_values = load_env_file()
            if token := env_file_values.get(env_var):
                return token

        # 3. Provider-specific credential stores
        if provider == ProviderName.CLAUDE:
            return extract_claude_cli_token()

        if provider == ProviderName.CODEX:
            from ai_usage.providers.codex import CodexCredentialStore

            store = CodexCredentialStore()
            creds = store.load()
            return creds.access_token if creds else None

        if provider == ProviderName.ZAI:
            # Accept the CN key as a fallback for the same provider.
            if token := os.environ.get("ZHIPU_API_KEY"):
                return token
            if token := load_env_file().get("ZHIPU_API_KEY"):
                return token

            # Finally, reuse whatever ZCode / OpenCode / Claude Code already has.
            from ai_usage.providers.zai import ZaiCredentialStore

            found = ZaiCredentialStore().load()
            return found[0] if found else None

        return None

    def is_provider_configured(self, provider: ProviderName) -> bool:
        """
        Check if a provider has required credentials.

        Delegates to provider's is_configured() method for accurate detection.
        """
        # Import providers lazily to avoid circular imports
        from ai_usage.providers import (
            ClaudeOAuthProvider,
            OpenRouterUsageProvider,
            CodexProvider,
            ZaiProvider,
        )

        provider_map = {
            ProviderName.CLAUDE: ClaudeOAuthProvider,
            ProviderName.OPENROUTER: OpenRouterUsageProvider,
            ProviderName.CODEX: CodexProvider,
            ProviderName.ZAI: ZaiProvider,
        }

        provider_class = provider_map.get(provider)
        if provider_class:
            return provider_class().is_configured()

        return False

    def get_provider_status(self, provider: ProviderName) -> dict[str, Any]:
        """Get detailed status for a provider."""
        info = self.PROVIDER_INFO.get(provider, {})
        env_var = self.ENV_VARS.get(provider, "UNKNOWN")
        configured = self.is_provider_configured(provider)

        return {
            "provider": provider.value,
            "name": info.get("name", provider.value),
            "description": info.get("description", ""),
            "official": info.get("official", False),
            "note": info.get("note", ""),
            "env_var": env_var,
            "configured": configured,
            "token_preview": self._get_token_preview(provider) if configured else None,
        }

    def get_all_provider_status(self) -> list[dict[str, Any]]:
        """Get status for all providers."""
        return [self.get_provider_status(p) for p in ProviderName]

    def _get_token_preview(self, provider: ProviderName) -> str | None:
        """Get a safe preview of the token (first/last few chars)."""
        token = self.get_token(provider)
        if not token or len(token) < 12:
            return None
        return f"{token[:8]}...{token[-4:]}"

    def get_env_var_help(self) -> str:
        """Get help text for setting up environment variables."""
        lines = ["Required environment variables:\n"]

        for provider in ProviderName:
            env_var = self.ENV_VARS.get(provider)
            info = self.PROVIDER_INFO.get(provider, {})
            configured = self.is_provider_configured(provider)
            status = "[OK]" if configured else "[NOT SET]"

            lines.append(f"  {env_var}  {status}")
            lines.append(f"    {info.get('description', '')}")
            if note := info.get("note"):
                lines.append(f"    Note: {note}")
            lines.append("")

        return "\n".join(lines)


# Global config instance
config = Config()
