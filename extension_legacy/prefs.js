/* AI Usage Extension preferences - GNOME Shell 42-44 (GTK4 / libadwaita 1.1).
 *
 * The GNOME 45+ build lives in ../extension/prefs.js and uses the newer
 * EntryRow / SwitchRow / SpinRow. Behaviour changes must be applied to both. */
const { Adw, Gio, GLib, GObject, Gtk } = imports.gi;

const CONFIG_DIR = GLib.get_home_dir() + '/.config/ai-usage';
const ENV_FILE_PATH = CONFIG_DIR + '/env';
const SETTINGS_PATH = CONFIG_DIR + '/extension.json';

const LEGACY_CONFIG_DIR = GLib.get_home_dir() + '/.config/usage-tui';
const LEGACY_ENV_FILE_PATH = LEGACY_CONFIG_DIR + '/env';
const LEGACY_SETTINGS_PATH = LEGACY_CONFIG_DIR + '/extension.json';

const CLI_CANDIDATES = ['ai-usage', 'usage-tui'];

const PROVIDERS = ['claude', 'codex', 'zai', 'openrouter'];

const PANEL_WINDOWS = [
    {id: '5h', label: '5h only'},
    {id: '7d', label: '7d only'},
    {id: 'both', label: 'Both (5h · 7d)'},
];

const ZAI_PLATFORMS = [
    {id: 'zai', label: 'Global (api.z.ai)'},
    {id: 'zhipu', label: 'China (open.bigmodel.cn)'},
];

/* Keys the user can type in. Credential-store providers are described in the
 * status group instead, since they authenticate through their own CLI. */
const KEY_FIELDS = [
    {
        name: 'ZAI_API_KEY',
        title: 'Z.ai API key',
        subtitle: 'GLM Coding Plan / ZCode quota - found automatically when ZCode is installed',
        link: 'https://z.ai/manage-apikey/apikey-list',
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
        subtitle: 'Optional - read from the Claude CLI automatically',
        link: null,
    },
    {
        name: 'CODEX_ACCESS_TOKEN',
        title: 'Codex access token',
        subtitle: 'Optional - read from ~/.codex/auth.json automatically',
        link: null,
    },
];

function _readTextFile(path) {
    try {
        let [ok, contents] = GLib.file_get_contents(path);
        if (!ok || !contents)
            return null;
        return (contents instanceof Uint8Array)
            ? new TextDecoder('utf-8').decode(contents)
            : contents.toString();
    } catch (_e) {
        return null;
    }
}

function _parseEnvText(text) {
    let env = {};
    for (let line of text.split('\n')) {
        let trimmed = line.trim();
        if (!trimmed || trimmed.startsWith('#'))
            continue;
        let match = trimmed.match(/^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
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

function _loadEnv() {
    let text = _readTextFile(ENV_FILE_PATH);
    if (text === null)
        text = _readTextFile(LEGACY_ENV_FILE_PATH);
    return text ? _parseEnvText(text) : {};
}

/* Rewrite only the keys we manage, preserving comments and unknown lines. */
function _saveEnv(updates) {
    GLib.mkdir_with_parents(CONFIG_DIR, 0o755);

    let text = _readTextFile(ENV_FILE_PATH);
    if (text === null)
        text = _readTextFile(LEGACY_ENV_FILE_PATH) || '';

    let lines = text.split('\n');
    let written = new Set();
    let output = [];

    for (let line of lines) {
        let match = line.trim().match(/^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=/);
        let name = match ? match[1] : null;

        if (name && Object.prototype.hasOwnProperty.call(updates, name)) {
            written.add(name);
            if (updates[name])
                output.push(`${name}=${updates[name]}`);
            /* Empty value drops the line entirely. */
            continue;
        }

        output.push(line);
    }

    while (output.length > 0 && output[output.length - 1].trim() === '')
        output.pop();

    for (let [name, value] of Object.entries(updates)) {
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
    } catch (_e) {}
}

function _loadSettings() {
    let text = _readTextFile(SETTINGS_PATH) || _readTextFile(LEGACY_SETTINGS_PATH);
    if (!text)
        return {};
    try {
        return JSON.parse(text) || {};
    } catch (_e) {
        return {};
    }
}

function _saveSettings(settings) {
    GLib.mkdir_with_parents(CONFIG_DIR, 0o755);
    GLib.file_set_contents(SETTINGS_PATH, JSON.stringify(settings, null, 2));
}

/* Same rule as the extension: keep known names in their saved order, then
 * append anything the setting forgot. */
function _sanitizeOrder(value) {
    if (!Array.isArray(value))
        return PROVIDERS.slice();

    let ordered = value.filter((name, index) =>
        PROVIDERS.includes(name) && value.indexOf(name) === index);

    for (let name of PROVIDERS) {
        if (!ordered.includes(name))
            ordered.push(name);
    }

    return ordered;
}

function _cliPath() {
    for (let candidate of CLI_CANDIDATES) {
        let found = GLib.find_program_in_path(candidate);
        if (found)
            return found;
    }
    return null;
}

function _openTerminal(command) {
    const terminals = [
        ['gnome-terminal', '--', 'bash', '-c', `${command}; read -p "Press Enter to close"`],
        ['kgx', '-e', `bash -c '${command}; read -p "Press Enter to close"'`],
        ['xterm', '-e', `bash -c '${command}; read -p "Press Enter to close"'`],
    ];

    for (let argv of terminals) {
        if (!GLib.find_program_in_path(argv[0]))
            continue;
        try {
            Gio.Subprocess.new(argv, Gio.SubprocessFlags.NONE);
            return true;
        } catch (_e) {}
    }
    return false;
}

function init() {
}

function fillPreferencesWindow(window) {
    const env = _loadEnv();
    const settings = _loadSettings();

    let pendingEnv = {};
    let saveTimeout = null;

    const toast = (text) => {
        if (window.add_toast && Adw.Toast)
            window.add_toast(new Adw.Toast({title: text, timeout: 2}));
    };

    const flushEnv = () => {
        saveTimeout = null;
        if (Object.keys(pendingEnv).length === 0)
            return GLib.SOURCE_REMOVE;
        let updates = pendingEnv;
        pendingEnv = {};
        try {
            _saveEnv(updates);
            toast('Keys saved');
        } catch (e) {
            toast(`Could not save keys: ${e.message}`);
        }
        return GLib.SOURCE_REMOVE;
    };

    const queueEnvSave = (name, value) => {
        pendingEnv[name] = value;
        if (saveTimeout)
            GLib.source_remove(saveTimeout);
        saveTimeout = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 800, flushEnv);
    };

    /* Save anything still buffered when the window goes away. */
    window.connect('close-request', () => {
        if (saveTimeout) {
            GLib.source_remove(saveTimeout);
            saveTimeout = null;
        }
        flushEnv();
        return false;
    });

    /* ---------------------------------------------------------------- Keys */
    const keysPage = new Adw.PreferencesPage({
        title: 'API Keys',
        icon_name: 'dialog-password-symbolic',
    });

    const keysGroup = new Adw.PreferencesGroup({
        title: 'Provider credentials',
        description: 'Saved to ~/.config/ai-usage/env (readable only by you).',
    });

    /* Every key row gets the same trailing geometry - a fixed-width entry plus a
     * fixed-width link slot - so the fields line up whether or not the provider
     * has a "get a key" page. */
    const ENTRY_WIDTH = 260;
    const LINK_SLOT_WIDTH = 32;

    for (let field of KEY_FIELDS) {
        let row = new Adw.ActionRow({
            title: field.title,
            subtitle: field.subtitle,
        });

        let entry = new Gtk.PasswordEntry({
            show_peek_icon: true,
            valign: Gtk.Align.CENTER,
            hexpand: false,
            width_request: ENTRY_WIDTH,
            text: env[field.name] || '',
            placeholder_text: 'not set',
        });
        entry.connect('changed', () => {
            queueEnvSave(field.name, entry.get_text().trim());
        });

        let linkSlot = new Gtk.Box({
            valign: Gtk.Align.CENTER,
            halign: Gtk.Align.END,
            width_request: LINK_SLOT_WIDTH,
        });

        if (field.link) {
            let link = new Gtk.Button({
                icon_name: 'web-browser-symbolic',
                tooltip_text: `Open ${field.link}`,
                valign: Gtk.Align.CENTER,
                css_classes: ['flat'],
            });
            link.connect('clicked', () => {
                Gtk.show_uri(window, field.link, 0);
            });
            linkSlot.append(link);
        }

        let suffix = new Gtk.Box({
            orientation: Gtk.Orientation.HORIZONTAL,
            spacing: 6,
            valign: Gtk.Align.CENTER,
            halign: Gtk.Align.END,
        });
        suffix.append(entry);
        suffix.append(linkSlot);

        row.add_suffix(suffix);
        keysGroup.add(row);
    }

    /* Z.ai platform selector */
    const zaiPlatformRow = new Adw.ComboRow({
        title: 'Z.ai platform',
        subtitle: 'Which host serves your coding plan',
        model: Gtk.StringList.new(ZAI_PLATFORMS.map(p => p.label)),
    });
    let currentPlatform = (env.ZAI_PLATFORM || 'zai').toLowerCase();
    let platformIndex = ZAI_PLATFORMS.findIndex(p => p.id === currentPlatform);
    zaiPlatformRow.selected = platformIndex === -1 ? 0 : platformIndex;
    zaiPlatformRow.connect('notify::selected', () => {
        queueEnvSave('ZAI_PLATFORM', ZAI_PLATFORMS[zaiPlatformRow.selected].id);
    });
    keysGroup.add(zaiPlatformRow);

    keysPage.add(keysGroup);

    /* CLI-managed providers */
    const cliGroup = new Adw.PreferencesGroup({
        title: 'CLI-managed logins',
        description: 'Claude and Codex reuse the credentials their own CLI stores.',
    });

    const addCommandRow = (group, title, subtitle, buttonLabel, command) => {
        let row = new Adw.ActionRow({title, subtitle});
        let button = new Gtk.Button({
            label: buttonLabel,
            valign: Gtk.Align.CENTER,
        });
        button.connect('clicked', () => {
            if (!_openTerminal(command))
                toast('No terminal emulator found');
        });
        row.add_suffix(button);
        group.add(row);
        return row;
    };

    addCommandRow(cliGroup, 'Claude Code', 'Reads ~/.claude/.credentials.json',
        'Set up token', 'claude setup-token');
    addCommandRow(cliGroup, 'OpenAI Codex', 'Reads ~/.codex/auth.json',
        'Log in', 'codex login');

    keysPage.add(cliGroup);
    window.add(keysPage);

    /* ------------------------------------------------------------ Display */
    const displayPage = new Adw.PreferencesPage({
        title: 'Display',
        icon_name: 'preferences-system-symbolic',
    });

    const panelGroup = new Adw.PreferencesGroup({
        title: 'Panel',
        description: 'Providers with a single window show that one; money-based providers '
            + '(OpenRouter) show the amount spent.',
    });

    const providerRow = new Adw.ComboRow({
        title: 'Pinned provider',
        model: Gtk.StringList.new(PROVIDERS),
    });
    let providerIndex = PROVIDERS.indexOf(settings.panelProvider || 'claude');
    providerRow.selected = providerIndex === -1 ? 0 : providerIndex;
    providerRow.connect('notify::selected', () => {
        settings.panelProvider = PROVIDERS[providerRow.selected];
        _saveSettings(settings);
    });
    panelGroup.add(providerRow);

    const windowRow = new Adw.ComboRow({
        title: 'Window shown in the panel',
        subtitle: 'Both are separated by a bullet, e.g. 46% · 59%',
        model: Gtk.StringList.new(PANEL_WINDOWS.map(w => w.label)),
    });
    let windowIndex = PANEL_WINDOWS.findIndex(w => w.id === (settings.panelWindows || 'both'));
    windowRow.selected = windowIndex === -1 ? 2 : windowIndex;
    windowRow.connect('notify::selected', () => {
        settings.panelWindows = PANEL_WINDOWS[windowRow.selected].id;
        _saveSettings(settings);
    });
    panelGroup.add(windowRow);

    const refreshRow = new Adw.ActionRow({
        title: 'Refresh interval',
        subtitle: 'Seconds between automatic updates',
    });
    const refreshSpin = Gtk.SpinButton.new_with_range(30, 3600, 30);
    refreshSpin.valign = Gtk.Align.CENTER;
    refreshSpin.value = settings.refreshSeconds || 300;
    refreshSpin.connect('value-changed', () => {
        settings.refreshSeconds = refreshSpin.get_value_as_int();
        _saveSettings(settings);
    });
    refreshRow.add_suffix(refreshSpin);
    panelGroup.add(refreshRow);

    displayPage.add(panelGroup);

    /* ----------------------------------------------------------- Providers */
    const providersGroup = new Adw.PreferencesGroup({
        title: 'Providers',
        description: 'Cards appear in this order. The pinned provider keeps feeding '
            + 'the panel even when its card is hidden.',
    });

    let order = _sanitizeOrder(settings.providerOrder);
    let visible = Array.isArray(settings.visibleProviders)
        ? settings.visibleProviders.filter(name => PROVIDERS.includes(name))
        : PROVIDERS.slice();

    /* Rows are rebuilt from scratch on every move: with four providers that is
     * cheaper than juggling widget positions. */
    let providerRows = [];

    const rebuildProviderRows = () => {
        for (let row of providerRows)
            providersGroup.remove(row);
        providerRows = [];

        order.forEach((name, index) => {
            let row = new Adw.ActionRow({title: name});

            let up = new Gtk.Button({
                icon_name: 'go-up-symbolic',
                valign: Gtk.Align.CENTER,
                sensitive: index > 0,
                tooltip_text: 'Move up',
                css_classes: ['flat'],
            });
            up.connect('clicked', () => moveProvider(index, -1));

            let down = new Gtk.Button({
                icon_name: 'go-down-symbolic',
                valign: Gtk.Align.CENTER,
                sensitive: index < order.length - 1,
                tooltip_text: 'Move down',
                css_classes: ['flat'],
            });
            down.connect('clicked', () => moveProvider(index, 1));

            let toggle = new Gtk.Switch({
                valign: Gtk.Align.CENTER,
                active: visible.includes(name),
            });
            toggle.connect('notify::active', () => {
                visible = PROVIDERS.filter(other => other === name
                    ? toggle.get_active() : visible.includes(other));
                settings.visibleProviders = visible;
                _saveSettings(settings);
            });

            row.add_suffix(up);
            row.add_suffix(down);
            row.add_suffix(toggle);
            row.activatable_widget = toggle;

            providersGroup.add(row);
            providerRows.push(row);
        });
    };

    const moveProvider = (index, delta) => {
        let target = index + delta;
        if (target < 0 || target >= order.length)
            return;
        let next = order.slice();
        [next[index], next[target]] = [next[target], next[index]];
        order = next;
        settings.providerOrder = order;
        _saveSettings(settings);
        rebuildProviderRows();
    };

    rebuildProviderRows();
    displayPage.add(providersGroup);

    /* -------------------------------------------------------------- Status */
    const statusGroup = new Adw.PreferencesGroup({
        title: 'Backend',
    });

    const cli = _cliPath();
    const cliRow = new Adw.ActionRow({
        title: 'ai-usage CLI',
        subtitle: cli || 'Not found in PATH - install it with pipx',
    });
    statusGroup.add(cliRow);

    addCommandRow(statusGroup, 'Check connectivity',
        'Runs the doctor command for every provider',
        'Run doctor', `${cli || CLI_CANDIDATES[0]} doctor`);

    addCommandRow(statusGroup, 'Open the TUI',
        'Full AI Usage TUI in a terminal',
        'Open', `${cli || CLI_CANDIDATES[0]} tui`);

    displayPage.add(statusGroup);
    window.add(displayPage);
}
