/* AI Usage Extension preferences - GNOME Shell 45+ (ES modules, libadwaita 1.4+).
 *
 * The GNOME 42-44 build lives in ../extension_legacy/prefs.js and targets
 * libadwaita 1.1, where EntryRow / SwitchRow / SpinRow do not exist yet.
 * Behaviour changes must be applied to both.
 */
import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Gtk from 'gi://Gtk';

import { ExtensionPreferences } from 'resource:///org/gnome/shell/extensions/prefs.js';

const CONFIG_DIR = GLib.get_home_dir() + '/.config/ai-usage';
const ENV_FILE_PATH = CONFIG_DIR + '/env';
const SETTINGS_PATH = CONFIG_DIR + '/extension.json';

const CLI_NAME = 'ai-usage';

const PROVIDERS = ['claude', 'codex', 'zai', 'kimi', 'openrouter'];

const PANEL_WINDOWS = [
    { id: '5h', label: '5h only' },
    { id: '7d', label: '7d only' },
    { id: 'both', label: 'Both (5h · 7d)' },
];

const ZAI_PLATFORMS = [
    { id: 'zai', label: 'Global (api.z.ai)' },
    { id: 'zhipu', label: 'China (open.bigmodel.cn)' },
];

/* Keys the user can type in. Claude and Codex normally authenticate through
 * their own CLI, so theirs are optional overrides. */
const KEY_FIELDS = [
    {
        name: 'ZAI_API_KEY',
        title: 'Z.ai API key',
        subtitle: 'GLM Coding Plan / ZCode - usually detected automatically',
        link: 'https://z.ai/manage-apikey/apikey-list',
    },
    {
        name: 'KIMI_API_KEY',
        title: 'Kimi Code API key',
        subtitle: 'Coding plan (sk-kimi-...) - usually detected automatically',
        link: 'https://www.kimi.com/code',
    },
    {
        name: 'OPENROUTER_API_KEY',
        title: 'OpenRouter API key',
        subtitle: 'Credits and spend limit',
        link: 'https://openrouter.ai/keys',
    },
    {
        name: 'CLAUDE_CODE_OAUTH_TOKEN',
        title: 'Claude OAuth token',
        subtitle: 'Optional - read from the Claude CLI',
        link: null,
    },
    {
        name: 'CODEX_ACCESS_TOKEN',
        title: 'Codex access token',
        subtitle: 'Optional - read from ~/.codex/auth.json',
        link: null,
    },
];

const SAVE_DEBOUNCE_MS = 800;

function readTextFile(path) {
    try {
        const [ok, contents] = GLib.file_get_contents(path);
        if (!ok || !contents)
            return null;
        return new TextDecoder('utf-8').decode(contents);
    } catch {
        return null;
    }
}

function parseEnvText(text) {
    const env = {};

    for (const line of text.split('\n')) {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith('#'))
            continue;

        const match = trimmed.match(/^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
        if (!match)
            continue;

        let value = match[2].trim();
        if (value.endsWith(';'))
            value = value.slice(0, -1).trim();
        if ((value.startsWith('"') && value.endsWith('"')) ||
            (value.startsWith("'") && value.endsWith("'")))
            value = value.slice(1, -1);

        env[match[1]] = value;
    }

    return env;
}

function loadEnv() {
    const text = readTextFile(ENV_FILE_PATH);
    return text ? parseEnvText(text) : {};
}

/* Rewrite only the keys we manage, preserving comments and unknown lines. */
function saveEnv(updates) {
    GLib.mkdir_with_parents(CONFIG_DIR, 0o755);

    const text = readTextFile(ENV_FILE_PATH) || '';

    const written = new Set();
    const output = [];

    for (const line of text.split('\n')) {
        const match = line.trim().match(/^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=/);
        const name = match ? match[1] : null;

        if (name && Object.hasOwn(updates, name)) {
            written.add(name);
            if (updates[name])
                output.push(`${name}=${updates[name]}`);
            /* Empty value drops the line entirely. */
            continue;
        }

        output.push(line);
    }

    while (output.length > 0 && output.at(-1).trim() === '')
        output.pop();

    for (const [name, value] of Object.entries(updates)) {
        if (written.has(name) || !value)
            continue;
        output.push(`${name}=${value}`);
    }

    let body = output.join('\n').replace(/\n{3,}/g, '\n\n').replace(/^\n+/, '');
    if (!body.endsWith('\n'))
        body += '\n';

    GLib.file_set_contents(ENV_FILE_PATH, body);
    /* Keys are secrets: keep the file owner-only. */
    try {
        Gio.File.new_for_path(ENV_FILE_PATH).set_attribute_uint32(
            'unix::mode', 0o600, Gio.FileQueryInfoFlags.NONE, null);
    } catch {}
}

function loadSettings() {
    const text = readTextFile(SETTINGS_PATH);
    if (!text)
        return {};
    try {
        return JSON.parse(text) || {};
    } catch {
        return {};
    }
}

function saveSettings(settings) {
    GLib.mkdir_with_parents(CONFIG_DIR, 0o755);
    GLib.file_set_contents(SETTINGS_PATH, JSON.stringify(settings, null, 2));
}

/* Keep known providers in their saved order, then append anything the setting
 * forgot, so a stale list never hides a provider. */
function sanitizeOrder(value) {
    if (!Array.isArray(value))
        return [...PROVIDERS];

    const ordered = value.filter((name, index) =>
        PROVIDERS.includes(name) && value.indexOf(name) === index);

    for (const name of PROVIDERS) {
        if (!ordered.includes(name))
            ordered.push(name);
    }

    return ordered;
}

function cliPath() {
    return GLib.find_program_in_path(CLI_NAME);
}

function openTerminal(command) {
    const wrapped = `${command}; read -p "Press Enter to close"`;
    const terminals = [
        ['gnome-terminal', '--', 'bash', '-c', wrapped],
        ['kgx', '-e', `bash -c '${wrapped}'`],
        ['ptyxis', '--', 'bash', '-c', wrapped],
        ['xterm', '-e', `bash -c '${wrapped}'`],
    ];

    for (const argv of terminals) {
        if (!GLib.find_program_in_path(argv[0]))
            continue;
        try {
            Gio.Subprocess.new(argv, Gio.SubprocessFlags.NONE);
            return true;
        } catch {}
    }
    return false;
}

export default class AiUsagePreferences extends ExtensionPreferences {

    fillPreferencesWindow(window) {
        this._env = loadEnv();
        this._settings = loadSettings();
        this._pendingEnv = {};
        this._saveTimeout = null;

        window.add(this._buildKeysPage(window));
        window.add(this._buildDisplayPage(window));

        /* Flush anything still buffered when the window goes away. */
        window.connect('close-request', () => {
            if (this._saveTimeout) {
                GLib.Source.remove(this._saveTimeout);
                this._saveTimeout = null;
            }
            this._flushEnv(window);
            return false;
        });
    }

    _toast(window, title) {
        window.add_toast(new Adw.Toast({ title, timeout: 2 }));
    }

    _flushEnv(window) {
        this._saveTimeout = null;
        if (Object.keys(this._pendingEnv).length === 0)
            return GLib.SOURCE_REMOVE;

        const updates = this._pendingEnv;
        this._pendingEnv = {};

        try {
            saveEnv(updates);
            this._toast(window, 'Keys saved');
        } catch (error) {
            this._toast(window, `Could not save keys: ${error.message}`);
        }

        return GLib.SOURCE_REMOVE;
    }

    /* Typing shouldn't hit the disk on every keystroke. */
    _queueEnvSave(window, name, value) {
        this._pendingEnv[name] = value;

        if (this._saveTimeout)
            GLib.Source.remove(this._saveTimeout);

        this._saveTimeout = GLib.timeout_add(
            GLib.PRIORITY_DEFAULT, SAVE_DEBOUNCE_MS, () => this._flushEnv(window));
    }

    _buildKeysPage(window) {
        const page = new Adw.PreferencesPage({
            title: 'API Keys',
            icon_name: 'dialog-password-symbolic',
        });

        const keysGroup = new Adw.PreferencesGroup({
            title: 'Provider credentials',
            description: 'Saved to ~/.config/ai-usage/env (readable only by you).',
        });

        for (const field of KEY_FIELDS) {
            const row = new Adw.PasswordEntryRow({
                title: field.title,
                text: this._env[field.name] || '',
            });
            row.connect('changed', () => {
                this._queueEnvSave(window, field.name, row.get_text().trim());
            });

            if (field.link) {
                const link = new Gtk.Button({
                    icon_name: 'web-browser-symbolic',
                    tooltip_text: `Open ${field.link}`,
                    valign: Gtk.Align.CENTER,
                    css_classes: ['flat'],
                });
                link.connect('clicked', () => {
                    Gio.AppInfo.launch_default_for_uri(field.link, null);
                });
                row.add_suffix(link);
            }

            keysGroup.add(row);

            /* PasswordEntryRow has no subtitle, so the hint goes in a tooltip. */
            row.set_tooltip_text(field.subtitle);
        }

        keysGroup.add(this._buildZaiPlatformRow());
        page.add(keysGroup);

        const cliGroup = new Adw.PreferencesGroup({
            title: 'CLI-managed logins',
            description: 'Claude and Codex reuse the credentials their own CLI stores.',
        });
        cliGroup.add(this._buildCommandRow(window, 'Claude Code',
            'Reads ~/.claude/.credentials.json', 'Set up token', 'claude setup-token'));
        cliGroup.add(this._buildCommandRow(window, 'OpenAI Codex',
            'Reads ~/.codex/auth.json', 'Log in', 'codex login'));
        page.add(cliGroup);

        return page;
    }

    _buildZaiPlatformRow() {
        const row = new Adw.ComboRow({
            title: 'Z.ai platform',
            subtitle: 'Which host serves your coding plan',
            model: Gtk.StringList.new(ZAI_PLATFORMS.map(platform => platform.label)),
        });

        const current = (this._env.ZAI_PLATFORM || 'zai').toLowerCase();
        const index = ZAI_PLATFORMS.findIndex(platform => platform.id === current);
        row.selected = index === -1 ? 0 : index;

        row.connect('notify::selected', () => {
            /* Written straight through: a combo has no keystrokes to debounce. */
            try {
                saveEnv({ ZAI_PLATFORM: ZAI_PLATFORMS[row.selected].id });
            } catch {}
        });

        return row;
    }

    _buildCommandRow(window, title, subtitle, buttonLabel, command) {
        const row = new Adw.ActionRow({ title, subtitle });
        const button = new Gtk.Button({
            label: buttonLabel,
            valign: Gtk.Align.CENTER,
        });
        button.connect('clicked', () => {
            if (!openTerminal(command))
                this._toast(window, 'No terminal emulator found');
        });
        row.add_suffix(button);
        row.activatable_widget = button;
        return row;
    }

    _buildDisplayPage(window) {
        const page = new Adw.PreferencesPage({
            title: 'Display',
            icon_name: 'preferences-system-symbolic',
        });

        page.add(this._buildPanelGroup());
        page.add(this._buildProvidersGroup());
        page.add(this._buildBackendGroup(window));

        return page;
    }

    _buildPanelGroup() {
        const group = new Adw.PreferencesGroup({
            title: 'Panel',
            description: 'Providers with a single window show that one; money-based '
                + 'providers (OpenRouter) show the amount spent.',
        });

        const providerRow = new Adw.ComboRow({
            title: 'Pinned provider',
            model: Gtk.StringList.new(PROVIDERS),
        });
        const providerIndex = PROVIDERS.indexOf(this._settings.panelProvider || 'claude');
        providerRow.selected = providerIndex === -1 ? 0 : providerIndex;
        providerRow.connect('notify::selected', () => {
            this._settings.panelProvider = PROVIDERS[providerRow.selected];
            saveSettings(this._settings);
        });
        group.add(providerRow);

        const windowRow = new Adw.ComboRow({
            title: 'Window shown in the panel',
            subtitle: 'Both are separated by a bullet, e.g. 46% · 59%',
            model: Gtk.StringList.new(PANEL_WINDOWS.map(entry => entry.label)),
        });
        const windowIndex = PANEL_WINDOWS.findIndex(
            entry => entry.id === (this._settings.panelWindows || 'both'));
        windowRow.selected = windowIndex === -1 ? 2 : windowIndex;
        windowRow.connect('notify::selected', () => {
            this._settings.panelWindows = PANEL_WINDOWS[windowRow.selected].id;
            saveSettings(this._settings);
        });
        group.add(windowRow);

        const refreshRow = new Adw.SpinRow({
            title: 'Refresh interval',
            subtitle: 'Seconds between automatic updates',
            adjustment: new Gtk.Adjustment({
                lower: 30,
                upper: 3600,
                step_increment: 30,
                page_increment: 300,
                value: this._settings.refreshSeconds || 300,
            }),
        });
        refreshRow.connect('notify::value', () => {
            this._settings.refreshSeconds = refreshRow.get_value();
            saveSettings(this._settings);
        });
        group.add(refreshRow);

        return group;
    }

    _buildProvidersGroup() {
        const group = new Adw.PreferencesGroup({
            title: 'Providers',
            description: 'Cards appear in this order. The pinned provider keeps feeding '
                + 'the panel even when its card is hidden.',
        });

        let order = sanitizeOrder(this._settings.providerOrder);
        let visible = Array.isArray(this._settings.visibleProviders)
            ? this._settings.visibleProviders.filter(name => PROVIDERS.includes(name))
            : [...PROVIDERS];

        /* Rows are rebuilt on every move: with a handful of providers that is
         * cheaper than juggling widget positions. */
        let rows = [];

        const move = (index, delta) => {
            const target = index + delta;
            if (target < 0 || target >= order.length)
                return;

            const next = [...order];
            [next[index], next[target]] = [next[target], next[index]];
            order = next;

            this._settings.providerOrder = order;
            saveSettings(this._settings);
            rebuild();
        };

        const rebuild = () => {
            for (const row of rows)
                group.remove(row);
            rows = [];

            order.forEach((name, index) => {
                const row = new Adw.SwitchRow({
                    title: name,
                    active: visible.includes(name),
                });

                const up = new Gtk.Button({
                    icon_name: 'go-up-symbolic',
                    valign: Gtk.Align.CENTER,
                    sensitive: index > 0,
                    tooltip_text: 'Move up',
                    css_classes: ['flat'],
                });
                up.connect('clicked', () => move(index, -1));

                const down = new Gtk.Button({
                    icon_name: 'go-down-symbolic',
                    valign: Gtk.Align.CENTER,
                    sensitive: index < order.length - 1,
                    tooltip_text: 'Move down',
                    css_classes: ['flat'],
                });
                down.connect('clicked', () => move(index, 1));

                row.add_prefix(up);
                row.add_prefix(down);

                row.connect('notify::active', () => {
                    visible = PROVIDERS.filter(other => other === name
                        ? row.get_active() : visible.includes(other));
                    this._settings.visibleProviders = visible;
                    saveSettings(this._settings);
                });

                group.add(row);
                rows.push(row);
            });
        };

        rebuild();
        return group;
    }

    _buildBackendGroup(window) {
        const group = new Adw.PreferencesGroup({ title: 'Backend' });
        const cli = cliPath();

        group.add(new Adw.ActionRow({
            title: 'ai-usage CLI',
            subtitle: cli || 'Not found in PATH - install it with pipx',
        }));

        group.add(this._buildCommandRow(window, 'Check connectivity',
            'Runs the doctor command for every provider',
            'Run doctor', `${cli || CLI_NAME} doctor`));

        group.add(this._buildCommandRow(window, 'Open the TUI',
            'Full AI Usage TUI in a terminal',
            'Open', `${cli || CLI_NAME} tui`));

        return group;
    }
}
