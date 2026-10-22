"""Fail-on-revert checks for OpenCode integration surfaces."""

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OpenCodeExtensionIntegrationTests(unittest.TestCase):
    def test_both_extension_variants_register_catalog_and_refresh_guards(self) -> None:
        for directory in ("extension", "extension_legacy"):
            source = (ROOT / directory / "extension.js").read_text()
            self.assertIn("opencode: 'OpenCode Zen'", source)
            self.assertIn("'opencode-go': 'OpenCode Go'", source)
            self.assertIn("['5h', '7d', '30d', 'both']", source)
            self.assertIn("_queueCredentialRefresh()", source)
            self.assertIn("if (this._refreshActive)", source)
            self.assertIn("this._refreshPending = true", source)
            self.assertIn("_finishRefresh(generation)", source)
            self.assertIn("this._destroyed = true", source)

            settings_handler = source.split("_onSettingsFileChanged() {", 1)[1].split(
                "\n    _refreshData() {", 1
            )[0]
            self.assertNotIn("this._refreshData()", settings_handler)

    def test_both_preferences_variants_expose_products_and_credentials(self) -> None:
        for directory in ("extension", "extension_legacy"):
            source = (ROOT / directory / "prefs.js").read_text()
            for marker in (
                "OpenCode Zen",
                "OpenCode Go",
                "OPENCODE_API_KEY",
                "OPENCODE_WORKSPACE_ID",
                "OPENCODE_AUTH_COOKIE",
                "providerCatalogVersion",
                "30d only",
            ):
                self.assertIn(marker, source)

    def test_shared_assets_are_present_and_in_sync(self) -> None:
        modern = ROOT / "extension"
        legacy = ROOT / "extension_legacy"
        self.assertEqual(
            (modern / "stylesheet.css").read_bytes(),
            (legacy / "stylesheet.css").read_bytes(),
        )
        for icon in ("opencode.svg", "opencode-go.svg"):
            self.assertEqual(
                (modern / "icons" / icon).read_bytes(),
                (legacy / "icons" / icon).read_bytes(),
            )
        for selector in (
            ".ai-usage-label-opencode",
            ".ai-usage-label-opencode-go",
            ".ai-usage-provider-opencode",
            ".ai-usage-provider-opencode-go",
        ):
            self.assertIn(selector, (modern / "stylesheet.css").read_text())

    def test_metadata_mentions_opencode_in_both_variants(self) -> None:
        for directory in ("extension", "extension_legacy"):
            metadata = json.loads((ROOT / directory / "metadata.json").read_text())
            self.assertIn("OpenCode Zen/Go", metadata["description"])


class OpenCodePythonIntegrationTests(unittest.TestCase):
    def test_cli_and_tui_use_registry_and_generic_windows(self) -> None:
        cli = (ROOT / "ai_usage" / "cli.py").read_text()
        tui = (ROOT / "ai_usage" / "tui.py").read_text()
        self.assertIn("from ai_usage.providers import build_providers", cli)
        self.assertIn("from ai_usage.providers import build_providers", tui)
        self.assertIn('result.raw.get("windows")', cli)
        self.assertIn('result.raw.get("windows")', tui)
        self.assertIn("effective_window = provider.normalize_window", cli)
        self.assertIn("effective_window = provider.normalize_window", tui)


if __name__ == "__main__":
    unittest.main()
