"""CLI entry point for ai-usage."""

import asyncio
import json
import sys
from datetime import datetime, timezone

import click
from click.core import ParameterSource

from ai_usage.config import config
from ai_usage.providers import (
    ClaudeOAuthProvider,
    OpenRouterUsageProvider,
    CodexProvider,
    ZaiProvider,
)
from ai_usage.providers.base import BaseProvider, ProviderError, ProviderName, WindowPeriod


def get_providers() -> dict[ProviderName, BaseProvider]:
    """Get all available providers."""
    return {
        ProviderName.CLAUDE: ClaudeOAuthProvider(),
        ProviderName.CODEX: CodexProvider(),
        ProviderName.ZAI: ZaiProvider(),
        ProviderName.OPENROUTER: OpenRouterUsageProvider(),
    }


def parse_window(window: str) -> WindowPeriod:
    """Parse window string to WindowPeriod enum."""
    mapping = {
        "5h": WindowPeriod.HOUR_5,
        "7d": WindowPeriod.DAY_7,
        "30d": WindowPeriod.DAY_30,
    }
    if window not in mapping:
        raise click.BadParameter(f"Invalid window. Choose from: {', '.join(mapping.keys())}")
    return mapping[window]


def parse_provider(provider: str) -> ProviderName | None:
    """Parse provider string. Returns None for 'all'."""
    if provider == "all":
        return None
    try:
        return ProviderName(provider)
    except ValueError:
        valid = ", ".join([p.value for p in ProviderName] + ["all"])
        raise click.BadParameter(f"Invalid provider. Choose from: {valid}")


def _fetch_result(provider: BaseProvider, window: WindowPeriod):
    """Fetch provider metrics, converting errors into results."""
    try:
        return asyncio.run(provider.fetch(window))
    except ProviderError as exc:
        return provider._make_error_result(window=window, error=str(exc))
    except Exception as exc:
        return provider._make_error_result(window=window, error=f"Unexpected error: {exc}")


@click.group()
@click.version_option()
def main() -> None:
    """Usage metrics TUI for Claude, Codex, Z.ai and OpenRouter."""
    pass


@main.command()
@click.option(
    "--provider",
    "-p",
    default="all",
    help="Provider to query (claude, codex, zai, openrouter, all)",
)
@click.option(
    "--window",
    "-w",
    default="7d",
    help="Time window (5h, 7d, 30d)",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help="Output raw JSON instead of formatted text",
)
def show(provider: str, window: str, output_json: bool) -> None:
    """Show usage metrics for providers."""
    window_period = parse_window(window)
    provider_filter = parse_provider(provider)

    ctx = click.get_current_context()
    window_source = ctx.get_parameter_source("window")

    providers = get_providers()

    # Filter to specific provider if requested
    if provider_filter:
        if provider_filter not in providers:
            click.echo(f"Provider {provider_filter.value} not implemented yet.", err=True)
            sys.exit(1)
        providers = {provider_filter: providers[provider_filter]}

    results = {}
    for name, prov in providers.items():
        if not prov.is_configured():
            if not output_json:
                click.echo(f"\n{name.value}: Not configured")
                click.echo(f"  Set {config.ENV_VARS.get(name)} environment variable")
            continue

        # Show both 5h and 7d windows for quota-based providers when using default window
        show_dual_windows = (
            not output_json
            and window_source == ParameterSource.DEFAULT
            and name in {ProviderName.CLAUDE, ProviderName.ZAI}
        )

        if show_dual_windows:
            result_5h = _fetch_result(prov, WindowPeriod.HOUR_5)
            result_7d = _fetch_result(prov, WindowPeriod.DAY_7)
            results[name.value] = result_7d
            _print_result(name, result_5h, label="5h")
            _print_result(name, result_7d, label="7d")
        else:
            result = _fetch_result(prov, window_period)
            results[name.value] = result
            if not output_json:
                _print_result(name, result)

    if output_json:
        output = {k: v.model_dump(mode="json") for k, v in results.items()}
        click.echo(json.dumps(output, indent=2))


def _print_result(name: ProviderName, result, label: str | None = None) -> None:
    """Print a formatted result."""
    title = name.value.upper()
    if label:
        title = f"{title} ({label})"
    click.echo(f"\n{click.style(title, bold=True)}")
    click.echo("-" * 40)

    if result.is_error:
        click.echo(click.style(f"Error: {result.error}", fg="red"))
        return

    m = result.metrics

    # Usage percentage (Claude)
    if m.usage_percent is not None:
        pct = m.usage_percent
        color = "green" if pct < 50 else ("yellow" if pct < 80 else "red")
        bar = _progress_bar(pct)
        click.echo(f"Usage:    {bar} {click.style(f'{pct:.1f}%', fg=color)}")

    # Reset time
    if m.reset_at:
        from datetime import datetime, timezone

        delta = m.reset_at - datetime.now(timezone.utc)
        if delta.total_seconds() > 0:
            hours = int(delta.total_seconds() // 3600)
            mins = int((delta.total_seconds() % 3600) // 60)
            click.echo(f"Resets:   {hours}h {mins}m")

    # Cost
    if m.cost is not None:
        click.echo(f"Cost:     ${m.cost:.4f}")

    # Requests
    if m.requests is not None:
        click.echo(f"Requests: {m.requests:,}")

    # Tokens
    if m.input_tokens is not None or m.output_tokens is not None:
        total = (m.input_tokens or 0) + (m.output_tokens or 0)
        click.echo(
            f"Tokens:   {total:,} ({m.input_tokens or 0:,} in / {m.output_tokens or 0:,} out)"
        )


def _progress_bar(percent: float, width: int = 20) -> str:
    """Create a text progress bar."""
    filled = int(width * percent / 100)
    empty = width - filled
    return f"[{'#' * filled}{'-' * empty}]"


@main.command()
def doctor() -> None:
    """Check provider configuration and connectivity."""
    click.echo("AI Usage Doctor")
    click.echo("=" * 40)
    click.echo()

    providers = get_providers()
    all_ok = True

    for name, provider in providers.items():
        info = config.get_provider_status(name)

        click.echo(f"{click.style(info['name'], bold=True)}")

        # Check configuration
        if info["configured"]:
            click.echo(f"  Config:     {click.style('OK', fg='green')} ({info['token_preview']})")
        else:
            click.echo(f"  Config:     {click.style('MISSING', fg='red')}")
            click.echo(f"              Set: {info['env_var']}")
            all_ok = False
            click.echo()
            continue

        # Test connectivity
        click.echo("  Testing:    ", nl=False)
        result = _fetch_result(provider, WindowPeriod.HOUR_5)
        if result.is_error:
            click.echo(click.style(f"FAILED - {result.error}", fg="red"))
            all_ok = False
        else:
            click.echo(click.style("OK", fg="green"))

        # Show notes
        if info.get("note"):
            click.echo(f"  Note:       {info['note']}")

        click.echo()

    # Summary
    click.echo("-" * 40)
    if all_ok:
        click.echo(click.style("All providers healthy!", fg="green"))
    else:
        click.echo(click.style("Some providers need attention.", fg="yellow"))


@main.command()
def tui() -> None:
    """Launch the interactive TUI."""
    from ai_usage.tui import run_tui

    run_tui()


@main.command()
def env() -> None:
    """Show required environment variables."""
    click.echo(config.get_env_var_help())


@main.command()
def setup() -> None:
    """Interactive setup wizard for API keys."""
    from ai_usage.config import ENV_FILE_PATH, write_env_file

    click.echo()
    click.echo("AI Usage Setup")
    click.echo("=" * 40)
    click.echo()
    click.echo(f"Keys will be saved to: {ENV_FILE_PATH}")
    click.echo("Press Enter to skip any key.")
    click.echo()

    updates: dict[str, str] = {}

    # OpenRouter API key
    click.echo(click.style("OpenRouter", bold=True))
    click.echo("  Get your API key from: https://openrouter.ai/keys")
    openrouter_key = click.prompt("  OPENROUTER_API_KEY", default="", show_default=False).strip()
    if openrouter_key:
        updates["OPENROUTER_API_KEY"] = openrouter_key
        click.echo("  -> Set")
    else:
        click.echo("  -> Skipped")
    click.echo()

    # Z.ai API key
    click.echo(click.style("Z.ai", bold=True))
    click.echo("  Skip this if ZCode is installed - its credential is picked up automatically.")
    click.echo("  Otherwise get an API key from: https://z.ai/manage-apikey/apikey-list")
    click.echo("  For the CN platform use ZHIPU_API_KEY and set ZAI_PLATFORM=zhipu.")
    zai_key = click.prompt("  ZAI_API_KEY", default="", show_default=False).strip()
    if zai_key:
        updates["ZAI_API_KEY"] = zai_key
        click.echo("  -> Set")
    else:
        click.echo("  -> Skipped")
    click.echo()

    # Claude - print instructions only
    click.echo(click.style("Claude Code", bold=True))
    click.echo("  Uses Claude CLI credentials automatically.")
    click.echo("  To set up:")
    click.echo("    npm install -g @anthropics/claude")
    click.echo("    claude setup-token")
    click.echo()

    # Codex - print instructions only
    click.echo(click.style("OpenAI Codex", bold=True))
    click.echo("  Uses Codex CLI credentials automatically.")
    click.echo("  To set up:")
    click.echo("    npm install -g @openai/codex")
    click.echo("    codex")
    click.echo()

    # Write to env file
    if updates:
        write_env_file(updates)
        click.echo("-" * 40)
        click.echo(click.style("Configuration saved!", fg="green"))
        click.echo(f"File: {ENV_FILE_PATH}")
        click.echo()
        click.echo("Keys saved:")
        for key in updates:
            click.echo(f"  {key}: set")
    else:
        click.echo("-" * 40)
        click.echo("No keys provided. Nothing saved.")

    click.echo()
    click.echo("Next steps:")
    click.echo("  ai-usage show --json")
    click.echo("  ai-usage show")
    click.echo("  ai-usage doctor")
    click.echo("  ai-usage tui")


@main.command()
@click.option(
    "--provider",
    "-p",
    default="claude",
    help="Provider to login to (claude)",
)
def login(provider: str) -> None:
    """
    Authenticate with a provider.

    For Claude: Extracts token from existing Claude CLI installation.
    """
    if provider == "claude":
        _login_claude()
    else:
        click.echo(f"Login not supported for provider: {provider}")
        click.echo("Supported providers: claude")
        sys.exit(1)


def _login_claude() -> None:
    """Login for Claude provider."""
    from ai_usage.claude_cli_auth import ClaudeCLIAuth

    auth = ClaudeCLIAuth()

    if not auth.is_available():
        click.echo(click.style("\n Claude CLI credentials not found", fg="red"))
        click.echo()
        click.echo("To set up Claude CLI:")
        click.echo("  1. Install: npm install -g @anthropics/claude")
        click.echo("  2. Authenticate: claude setup-token")
        click.echo("  3. Then run 'ai-usage login' again")
        sys.exit(1)

    info = auth.get_token_info()

    if info["expired"]:
        click.echo(click.style("\n Token expired", fg="yellow"))
        click.echo()
        click.echo("Run this to refresh:")
        click.echo("  claude setup-token")
        sys.exit(1)

    token = auth.get_access_token()

    if token:
        click.echo()
        click.echo("=" * 60)
        click.echo(click.style(" Token extracted successfully!", fg="green", bold=True))
        click.echo()
        click.echo(f"  Token:      {token[:15]}...")
        if expires_in := info.get("expires_in_hours"):
            click.echo(f"  Expires in: {expires_in} hours")
        if scopes := info.get("scopes"):
            click.echo(f"  Scopes:     {', '.join(scopes)}")
        click.echo()
        click.echo("Token auto-loaded from ~/.claude/.credentials.json")
        click.echo("You can now run: ai-usage show --provider claude")
        click.echo("=" * 60)
    else:
        click.echo(click.style("\n Could not extract token", fg="red"))
        sys.exit(1)


@main.command()
@click.option(
    "--provider",
    "-p",
    default="all",
    help="Provider to refresh (claude, codex, all)",
)
@click.option(
    "--force",
    is_flag=True,
    help="Refresh even when the stored token is not old enough yet",
)
def refresh(provider: str, force: bool) -> None:
    """
    Refresh stored OAuth credentials.

    Claude credentials are owned by the Claude CLI, so this only re-reads them
    and reports their expiry. Codex tokens are refreshed against OpenAI's
    token endpoint and written back to auth.json.
    """
    targets = ["claude", "codex"] if provider == "all" else [provider]

    unknown = [t for t in targets if t not in ("claude", "codex")]
    if unknown:
        click.echo(f"Refresh not supported for provider: {', '.join(unknown)}")
        click.echo("Supported providers: claude, codex, all")
        sys.exit(1)

    failures = 0
    for target in targets:
        if target == "claude":
            failures += 0 if _refresh_claude() else 1
        else:
            failures += 0 if _refresh_codex(force) else 1

    if failures:
        sys.exit(1)


def _refresh_claude() -> bool:
    """Re-read Claude CLI credentials and report their state."""
    from ai_usage.claude_cli_auth import ClaudeCLIAuth

    auth = ClaudeCLIAuth()

    if not auth.is_available():
        click.echo(click.style("claude: credentials not found", fg="red"))
        click.echo("        Run 'claude setup-token' to create them.")
        return False

    info = auth.get_token_info()

    if info["expired"]:
        click.echo(click.style("claude: token expired", fg="yellow"))
        click.echo("        Run 'claude setup-token' (or open the Claude CLI) to renew it.")
        return False

    expires_in = info.get("expires_in_hours")
    suffix = f" (expires in {expires_in}h)" if expires_in else ""
    click.echo(click.style(f"claude: token valid{suffix}", fg="green"))
    return True


def _refresh_codex(force: bool) -> bool:
    """Refresh the Codex access token using its refresh token."""
    from ai_usage.providers.codex import CodexCredentialStore, CodexTokenRefresher

    store = CodexCredentialStore()
    credentials = store.load()

    if credentials is None or not credentials.access_token:
        click.echo(click.style("codex: credentials not found", fg="red"))
        click.echo("        Run 'codex' to authenticate.")
        return False

    if not credentials.refresh_token:
        click.echo(click.style("codex: no refresh token stored", fg="yellow"))
        click.echo("        CODEX_ACCESS_TOKEN-only setups cannot be refreshed.")
        return False

    if not force and not credentials.needs_refresh():
        age_days = (datetime.now(timezone.utc) - credentials.last_refresh).days
        click.echo(click.style(f"codex: token still fresh ({age_days}d old)", fg="green"))
        return True

    try:
        refreshed = asyncio.run(CodexTokenRefresher().refresh(credentials))
    except Exception as exc:
        click.echo(click.style(f"codex: refresh failed - {exc}", fg="red"))
        return False

    if refreshed.access_token == credentials.access_token and not force:
        click.echo(click.style("codex: token unchanged", fg="yellow"))
        return True

    store.save(refreshed)
    click.echo(click.style("codex: token refreshed", fg="green"))
    return True


if __name__ == "__main__":
    main()
