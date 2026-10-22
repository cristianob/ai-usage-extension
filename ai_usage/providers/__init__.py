"""Provider implementations for usage metrics."""

from ai_usage.providers.base import BaseProvider, ProviderName, ProviderResult, UsageMetrics
from ai_usage.providers.claude_oauth import ClaudeOAuthProvider
from ai_usage.providers.codex import CodexProvider
from ai_usage.providers.grok import GrokProvider
from ai_usage.providers.kimi import KimiProvider
from ai_usage.providers.opencode_go import OpenCodeGoProvider
from ai_usage.providers.opencode_zen import OpenCodeZenProvider
from ai_usage.providers.openrouter import OpenRouterUsageProvider
from ai_usage.providers.zai import ZaiProvider

PROVIDER_CLASSES = {
    ProviderName.CLAUDE: ClaudeOAuthProvider,
    ProviderName.CODEX: CodexProvider,
    ProviderName.ZAI: ZaiProvider,
    ProviderName.KIMI: KimiProvider,
    ProviderName.OPENCODE_ZEN: OpenCodeZenProvider,
    ProviderName.OPENCODE_GO: OpenCodeGoProvider,
    ProviderName.OPENROUTER: OpenRouterUsageProvider,
    ProviderName.GROK: GrokProvider,
}


def build_providers() -> dict[ProviderName, BaseProvider]:
    """Build one provider instance for every supported provider."""
    return {name: provider_class() for name, provider_class in PROVIDER_CLASSES.items()}


__all__ = [
    "BaseProvider",
    "UsageMetrics",
    "ProviderResult",
    "ProviderName",
    "ClaudeOAuthProvider",
    "OpenRouterUsageProvider",
    "CodexProvider",
    "ZaiProvider",
    "KimiProvider",
    "OpenCodeZenProvider",
    "OpenCodeGoProvider",
    "GrokProvider",
    "PROVIDER_CLASSES",
    "build_providers",
]
