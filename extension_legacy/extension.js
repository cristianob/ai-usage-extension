/* AI Usage Extension - GNOME Shell 42-44 (legacy imports / init()).
 *
 * The GNOME 45+ build lives in ../extension/extension.js. GNOME 45 replaced the
 * extension module system, so each shell generation has its own
 * implementation; behaviour changes must be applied to both. */
const { GLib, St, Gio, Clutter, GObject } = imports.gi;
const Main = imports.ui.main;
const PanelMenu = imports.ui.panelMenu;
const PopupMenu = imports.ui.popupMenu;
const ExtensionUtils = imports.misc.extensionUtils;
const Me = ExtensionUtils.getCurrentExtension();

const DEFAULT_REFRESH_SECONDS = 300;
const MIN_REFRESH_SECONDS = 30;

const CONFIG_DIR = GLib.get_home_dir() + '/.config/ai-usage';
const ENV_FILE_PATH = CONFIG_DIR + '/env';
const SETTINGS_PATH = CONFIG_DIR + '/extension.json';

const CLI_NAME = 'ai-usage';
const CLI_PATH_ENV_VAR = 'AI_USAGE_PATH';

const PROVIDER_ORDER = ['claude', 'codex', 'zai', 'openrouter'];

/* Providers whose quota is expressed as a percentage, not as money/credits. */
const PERCENT_QUOTA_PROVIDERS = ['claude', 'codex', 'zai'];

/* Providers measured in money: the panel shows the amount, not a percentage. */
const MONEY_PROVIDERS = ['openrouter'];

/* What the panel shows for a percentage-based provider. */
const PANEL_WINDOW_MODES = ['5h', '7d', 'both'];
const DEFAULT_PANEL_WINDOWS = 'both';

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

function _loadSettings() {
    let text = _readTextFile(SETTINGS_PATH);
    if (!text)
        return {};
    try {
        return JSON.parse(text) || {};
    } catch (_e) {
        return {};
    }
}

function _saveSettings(settings) {
    try {
        GLib.mkdir_with_parents(CONFIG_DIR, 0o755);
        GLib.file_set_contents(SETTINGS_PATH, JSON.stringify(settings, null, 2));
    } catch (_e) {}
}

function _parseEnvText(text) {
    let env = {};

    for (let line of text.split('\n')) {
        let trimmed = line.trim();
        if (!trimmed || trimmed.startsWith('#'))
            continue;

        let hashIndex = trimmed.indexOf('#');
        if (hashIndex !== -1) {
            let beforeHash = trimmed.slice(0, hashIndex);
            let singleQuotes = (beforeHash.match(/'/g) || []).length;
            let doubleQuotes = (beforeHash.match(/"/g) || []).length;
            if (singleQuotes % 2 === 0 && doubleQuotes % 2 === 0)
                trimmed = beforeHash.trim();
        }

        let match = trimmed.match(/^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
        if (!match)
            continue;

        let name = match[1];
        let value = match[2].trim();

        if (value.endsWith(';'))
            value = value.slice(0, -1).trim();

        if ((value.startsWith('"') && value.endsWith('"')) ||
            (value.startsWith("'") && value.endsWith("'")))
            value = value.slice(1, -1);

        if (value)
            env[name] = value;
    }

    return env;
}

function _loadEnvFromFile() {
    let text = _readTextFile(ENV_FILE_PATH);
    return text ? _parseEnvText(text) : {};
}

function _getCliPath() {
    let found = GLib.find_program_in_path(CLI_NAME);
    if (found)
        return found;

    let env = _loadEnvFromFile();
    return env[CLI_PATH_ENV_VAR] || CLI_NAME;
}

function _getProgressClass(pct) {
    if (pct < 50)
        return 'ai-usage-progress-ok';
    if (pct < 80)
        return 'ai-usage-progress-warning';
    return 'ai-usage-progress-danger';
}

function _isNumber(value) {
    return typeof value === 'number' && !Number.isNaN(value);
}

/* Turn a rate-limit window length in seconds into a short label. */
function _windowLabelFromSeconds(seconds) {
    if (!_isNumber(seconds) || seconds <= 0)
        return null;
    if (seconds % 604800 === 0) {
        let weeks = seconds / 604800;
        return weeks === 1 ? '7d' : `${weeks * 7}d`;
    }
    if (seconds % 86400 === 0)
        return `${seconds / 86400}d`;
    if (seconds % 3600 === 0)
        return `${seconds / 3600}h`;
    return `${Math.round(seconds / 60)}m`;
}

/* OpenRouter describes its spend window with a word, not a duration. */
function _windowLabelFromReset(reset) {
    switch (String(reset || '').toLowerCase()) {
    case 'daily':
        return '1d';
    case 'weekly':
        return '7d';
    case 'monthly':
        return '30d';
    default:
        return null;
    }
}

function _formatCountdown(resetTime, useDays) {
    if (resetTime === null || resetTime === undefined)
        return null;

    let resetDate = _isNumber(resetTime)
        ? new Date(resetTime * 1000)
        : new Date(resetTime);

    if (Number.isNaN(resetDate.getTime()))
        return null;

    let diffMs = resetDate - new Date();
    if (diffMs <= 0)
        return null;

    let days = Math.floor(diffMs / (1000 * 60 * 60 * 24));
    let hours = Math.floor((diffMs % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
    let mins = Math.floor((diffMs % (1000 * 60 * 60)) / (1000 * 60));

    if (useDays && days > 0)
        return `Resets in ${days}d ${hours}h ${mins}m`;
    return `Resets in ${days * 24 + hours}h ${mins}m`;
}

function _formatTokens(value) {
    if (!_isNumber(value))
        return null;
    if (value >= 1e9)
        return `${(value / 1e9).toFixed(2)}B`;
    if (value >= 1e6)
        return `${(value / 1e6).toFixed(1)}M`;
    if (value >= 1e3)
        return `${Math.round(value / 1e3)}k`;
    return `${value}`;
}

/* A quota bar whose fill is sized during allocation.
 *
 * Computing the fill from get_width() after the fact is unreliable: the value
 * read depends on when the callback runs, so a bar could keep a width derived
 * from the track's CSS minimum instead of its final size. Allocating the fill
 * inside vfunc_allocate uses the box the track actually got, every time it
 * changes, with no signals and no stale state. */
const ProgressBar = GObject.registerClass(
class ProgressBar extends St.Widget {

    _init() {
        super._init({
            style_class: 'ai-usage-progress-bg',
            x_expand: true,
            y_align: Clutter.ActorAlign.CENTER,
            layout_manager: new Clutter.FixedLayout(),
        });

        this._fraction = 0;
        this._fill = new St.Widget({style_class: 'ai-usage-progress-fill'});
        this.add_child(this._fill);
    }

    /* pct is 0-100; anything else empties the bar. */
    setPercent(pct, fillClass) {
        let fraction = _isNumber(pct) ? Math.max(0, Math.min(100, pct)) / 100 : 0;

        this._fill.style_class = `ai-usage-progress-fill ${fillClass}`;

        if (fraction !== this._fraction) {
            this._fraction = fraction;
            this.queue_relayout();
        }
    }

    vfunc_allocate(box) {
        super.vfunc_allocate(box);

        let width = box.get_width();
        let height = box.get_height();
        let filled = Math.round(width * this._fraction);

        /* Keep a small-but-nonzero percentage visible. */
        if (this._fraction > 0 && filled < 2)
            filled = 2;
        if (filled > width)
            filled = width;

        this._fill.allocate(new Clutter.ActorBox({
            x1: 0,
            y1: 0,
            x2: filled,
            y2: height,
        }));
    }
});

const UsageIndicator = GObject.registerClass(
class UsageIndicator extends PanelMenu.Button {

    _init() {
        super._init(0.0, 'AI Usage', false);

        this._timeout = null;
        this._usageData = {};
        this._providerRows = {};
        this._lastUpdated = null;
        this._settingsMonitor = null;
        this._envMonitor = null;

        this._settings = _loadSettings();
        this._panelProvider = this._settings.panelProvider || 'claude';
        this._panelWindows = this._sanitizePanelWindows(this._settings.panelWindows);
        this._providerOrder = this._sanitizeProviderOrder(this._settings.providerOrder);
        this._visibleProviders = this._sanitizeVisibleProviders(this._settings.visibleProviders);
        this._refreshSeconds = this._sanitizeRefreshSeconds(this._settings.refreshSeconds);

        this._buildPanelButton();
        this._buildPopupMenu();
        this._watchConfigFiles();
        this._refreshData();
        this._startAutoRefresh();
    }

    _sanitizePanelWindows(value) {
        return PANEL_WINDOW_MODES.includes(value) ? value : DEFAULT_PANEL_WINDOWS;
    }

    /* Card order. Unknown names are dropped and any provider the setting forgot
     * is appended in the built-in order, so a stale list never hides a card. */
    _sanitizeProviderOrder(value) {
        if (!Array.isArray(value))
            return PROVIDER_ORDER.slice();

        let ordered = value.filter((name, index) =>
            PROVIDER_ORDER.includes(name) && value.indexOf(name) === index);

        for (let name of PROVIDER_ORDER) {
            if (!ordered.includes(name))
                ordered.push(name);
        }

        return ordered;
    }

    /* Which providers get a card in the popup. Anything unrecognized is dropped;
     * a missing setting means "all of them". An explicit empty list is honoured. */
    _sanitizeVisibleProviders(value) {
        if (!Array.isArray(value))
            return PROVIDER_ORDER.slice();
        return value.filter(name => PROVIDER_ORDER.includes(name));
    }

    _isProviderVisible(providerName) {
        return this._visibleProviders.includes(providerName);
    }

    _sanitizeRefreshSeconds(value) {
        if (!_isNumber(value))
            return DEFAULT_REFRESH_SECONDS;
        return Math.max(MIN_REFRESH_SECONDS, Math.round(value));
    }

    _buildPanelButton() {
        this.add_style_class_name('ai-usage-button');

        this._panelBox = new St.BoxLayout({
            style_class: 'panel-status-menu-box',
        });

        this._providerIcon = new St.Icon({
            style_class: 'ai-usage-provider-icon',
            icon_size: 16,
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._updateProviderBadge();

        this._panelLabel = new St.Label({
            text: '...',
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'ai-usage-panel-label',
        });

        this._panelBox.add_child(this._providerIcon);
        this._panelBox.add_child(this._panelLabel);
        this.add_child(this._panelBox);
    }

    _updateProviderBadge() {
        if (!this._providerIcon)
            return;
        let path = `${Me.path}/icons/${this._panelProvider}.svg`;
        let file = Gio.File.new_for_path(path);
        if (!file.query_exists(null))
            file = Gio.File.new_for_path(`${Me.path}/icons/claude.svg`);
        this._providerIcon.gicon = new Gio.FileIcon({file});
    }

    _buildPopupMenu() {
        let headerBox = new St.BoxLayout({
            vertical: false,
            style_class: 'ai-usage-header',
        });

        let headerIcon = new St.Icon({
            icon_name: 'utilities-system-monitor-symbolic',
            icon_size: 20,
        });

        let headerLabel = new St.Label({
            text: 'AI Usage',
            style_class: 'ai-usage-header-label',
            y_align: Clutter.ActorAlign.CENTER,
        });

        headerBox.add_child(headerIcon);
        headerBox.add_child(headerLabel);

        let headerItem = new PopupMenu.PopupBaseMenuItem({reactive: false});
        headerItem.add_style_class_name('ai-usage-header-item');
        headerItem.add_child(headerBox);
        this.menu.addMenuItem(headerItem);

        let separator1 = new PopupMenu.PopupSeparatorMenuItem();
        separator1.style_class = 'ai-usage-separator';
        this.menu.addMenuItem(separator1);

        this._providersContainer = new St.BoxLayout({
            vertical: true,
            x_expand: true,
            x_align: Clutter.ActorAlign.FILL,
            style_class: 'ai-usage-providers',
        });

        this.menu.box.add_child(this._providersContainer);

        let separator2 = new PopupMenu.PopupSeparatorMenuItem();
        separator2.style_class = 'ai-usage-separator';
        this.menu.addMenuItem(separator2);

        /* Every display option lives in the preferences window; the popup keeps
         * only the two actions that make sense in place. */
        let refreshItem = new PopupMenu.PopupMenuItem('Refresh Now');
        refreshItem.connect('activate', () => {
            this._refreshData();
        });
        this.menu.addMenuItem(refreshItem);

        let settingsItem = new PopupMenu.PopupMenuItem('Settings & API Keys');
        settingsItem.connect('activate', () => {
            ExtensionUtils.openPrefs();
        });
        this.menu.addMenuItem(settingsItem);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._lastUpdatedItem = new PopupMenu.PopupMenuItem('Last updated: Never', {
            reactive: false,
        });
        this._lastUpdatedItem.label.style_class = 'ai-usage-last-updated';
        this.menu.addMenuItem(this._lastUpdatedItem);

    }

    /* ------------------------------------------------------------------
     * Window extraction
     * ------------------------------------------------------------------ */

    /* Normalize every provider shape into a list of quota windows:
     *   {key, label, pct, resetAt, useDays, detail}
     * Providers no longer need to have a 5h window (Codex is weekly-only),
     * so labels come from the data instead of from fixed positions. */
    _extractWindows(providerName, data) {
        const raw = data.raw || {};
        const windows = [];
        const seen = new Set();

        const push = (key, label, pct, resetAt, detail) => {
            if (!_isNumber(pct) || seen.has(key))
                return;
            seen.add(key);
            windows.push({
                key,
                label,
                pct,
                resetAt: resetAt === undefined ? null : resetAt,
                useDays: /d$/.test(label),
                detail: detail || null,
            });
        };

        /* Claude / Z.ai shape: named windows with utilization percentages. */
        const named = [
            ['5h', raw.five_hour],
            ['7d', raw.seven_day],
            ['30d', raw.thirty_day],
        ];
        for (let [key, entry] of named) {
            if (!entry)
                continue;
            push(key, key, entry.utilization, entry.resets_at,
                this._tokenDetail(entry));
        }

        /* Codex shape: rate_limit windows that describe their own length. */
        const rateLimit = raw.rate_limit || {};
        for (let windowName of ['primary_window', 'secondary_window']) {
            let entry = rateLimit[windowName];
            if (!entry)
                continue;
            let label = _windowLabelFromSeconds(entry.limit_window_seconds)
                || (windowName === 'primary_window' ? '5h' : '7d');
            push(label, label, entry.used_percent, entry.reset_at);
        }

        /* Spend-limit shape (OpenRouter): one window named by limit_reset. */
        if (windows.length === 0) {
            const metrics = data.metrics || {};
            let pct = this._usagePercentFromMetrics(metrics);
            if (_isNumber(pct)) {
                let label = _windowLabelFromReset((raw.data || {}).limit_reset)
                    || data.window || '';
                push(label || 'total', label, pct, metrics.reset_at || null);
            }
        }

        return windows;
    }

    _tokenDetail(entry) {
        let used = _formatTokens(entry.used_tokens);
        let total = _formatTokens(entry.total_tokens);
        if (used && total)
            return `${used} / ${total} tokens`;
        return null;
    }

    /* ------------------------------------------------------------------
     * Cards
     * ------------------------------------------------------------------ */

    _updateProviderCard(providerName, data) {
        let card = this._providerRows[providerName];

        if (!card) {
            card = this._createProviderCard(providerName);
            this._providerRows[providerName] = card;
            this._providersContainer.add_child(card.container);
        }

        this._populateProviderCard(card, providerName, data);
    }

    _createProviderCard(providerName) {
        let container = new St.BoxLayout({
            vertical: true,
            x_expand: true,
            x_align: Clutter.ActorAlign.FILL,
            style_class: `ai-usage-card ai-usage-provider-${providerName}`,
        });

        let header = new St.Label({
            text: providerName.toUpperCase(),
            style_class: `ai-usage-card-header ai-usage-label-${providerName}`,
        });
        container.add_child(header);

        let windowBars = new St.BoxLayout({
            vertical: true,
            x_expand: true,
            style_class: 'ai-usage-window-bars',
        });
        container.add_child(windowBars);

        let statsGrid = new St.BoxLayout({
            vertical: true,
            style_class: 'ai-usage-stats',
        });

        /* Start hidden: an empty label would still occupy a row. */
        let costLabel = new St.Label({style_class: 'ai-usage-cost', visible: false});
        let byokLabel = new St.Label({style_class: 'ai-usage-stat', visible: false});
        let requestsLabel = new St.Label({style_class: 'ai-usage-stat', visible: false});
        let tokensLabel = new St.Label({style_class: 'ai-usage-stat', visible: false});
        let resetsLabel = new St.Label({style_class: 'ai-usage-stat-reset', visible: false});
        let errorLabel = new St.Label({style_class: 'ai-usage-error', visible: false});

        statsGrid.add_child(costLabel);
        statsGrid.add_child(byokLabel);
        statsGrid.add_child(requestsLabel);
        statsGrid.add_child(tokensLabel);
        statsGrid.add_child(resetsLabel);
        statsGrid.add_child(errorLabel);

        container.add_child(statsGrid);

        return {
            container,
            header,
            windowBars,
            bars: [],
            costLabel,
            byokLabel,
            requestsLabel,
            tokensLabel,
            resetsLabel,
            errorLabel,
        };
    }

    _createWindowBar() {
        let barContainer = new St.BoxLayout({
            vertical: true,
            x_expand: true,
            style_class: 'ai-usage-window-bar',
        });

        let row = new St.BoxLayout({
            vertical: false,
            x_expand: true,
            style_class: 'ai-usage-window-row',
        });

        let label = new St.Label({
            text: '',
            style_class: 'ai-usage-window-label',
            y_align: Clutter.ActorAlign.CENTER,
        });

        let bar = new ProgressBar();

        let pctLabel = new St.Label({
            text: '',
            style_class: 'ai-usage-window-pct',
            y_align: Clutter.ActorAlign.CENTER,
        });

        let resetLabel = new St.Label({
            text: '',
            style_class: 'ai-usage-reset-label',
            visible: false,
        });

        row.add_child(label);
        row.add_child(bar);
        row.add_child(pctLabel);
        barContainer.add_child(row);
        barContainer.add_child(resetLabel);

        return {container: barContainer, row, bar, pctLabel, label, resetLabel};
    }

    _getWindowBar(card, index) {
        while (card.bars.length <= index) {
            let bar = this._createWindowBar();
            card.bars.push(bar);
            card.windowBars.add_child(bar.container);
        }
        return card.bars[index];
    }

    _populateProviderCard(card, providerName, data) {
        const metrics = data.metrics || {};
        const raw = data.raw || {};

        if (data.error !== null && data.error !== undefined) {
            card.errorLabel.text = `⚠️ ${data.error}`;
            card.errorLabel.show();

            /* Empty labels still claim a line each, so hide them outright -
             * an errored card should be only as tall as its message. */
            for (let label of [card.costLabel, card.byokLabel, card.requestsLabel,
                card.tokensLabel, card.resetsLabel]) {
                label.text = '';
                label.hide();
            }

            card.windowBars.hide();
            for (let bar of card.bars)
                bar.container.hide();
            return;
        }

        card.errorLabel.hide();

        let windows = this._extractWindows(providerName, data);

        windows.forEach((win, index) => {
            let bar = this._getWindowBar(card, index);
            bar.label.text = win.label || '';
            bar.pctLabel.text = `${win.pct.toFixed(1)}%`;
            bar.bar.setPercent(win.pct, _getProgressClass(win.pct));

            let countdown = _formatCountdown(win.resetAt, win.useDays);
            let details = [countdown, win.detail].filter(t => t);
            if (details.length > 0) {
                bar.resetLabel.text = details.join(' · ');
                bar.resetLabel.show();
            } else {
                bar.resetLabel.text = '';
                bar.resetLabel.hide();
            }

            bar.container.show();
        });

        for (let i = windows.length; i < card.bars.length; i++)
            card.bars[i].container.hide();

        if (windows.length > 0)
            card.windowBars.show();
        else
            card.windowBars.hide();

        const setLabel = (label, text) => {
            if (text) { label.text = text; label.show(); }
            else { label.text = ''; label.hide(); }
        };

        setLabel(card.costLabel, this._costText(providerName, metrics, raw));
        setLabel(card.byokLabel, this._byokText(providerName, data));

        if (_isNumber(metrics.requests))
            setLabel(card.requestsLabel, `${metrics.requests.toLocaleString()} requests`);
        else
            setLabel(card.requestsLabel, '');

        if (PERCENT_QUOTA_PROVIDERS.includes(providerName)) {
            /* Token counts are already shown next to their window bar. */
            setLabel(card.tokensLabel, '');
        } else if (_isNumber(metrics.input_tokens) || _isNumber(metrics.output_tokens)) {
            let total = (metrics.input_tokens || 0) + (metrics.output_tokens || 0);
            setLabel(card.tokensLabel, `${total.toLocaleString()} tokens`);
        } else {
            setLabel(card.tokensLabel, '');
        }

        /* The bars carry their own countdown, so only show the summary reset
         * line when there is no bar to hang it off. */
        if (windows.length === 0)
            setLabel(card.resetsLabel, _formatCountdown(metrics.reset_at, true));
        else
            setLabel(card.resetsLabel, '');
    }

    _usagePercentFromMetrics(metrics) {
        if (_isNumber(metrics.usage_percent))
            return metrics.usage_percent;
        if (_isNumber(metrics.limit) && metrics.limit > 0 && _isNumber(metrics.remaining))
            return ((metrics.limit - metrics.remaining) / metrics.limit) * 100;
        return null;
    }

    _costText(providerName, metrics, raw) {
        if (providerName === 'zai') {
            let level = raw.plan_level;
            return level ? `Plan: ${String(level).toUpperCase()}` : '';
        }

        if (providerName === 'codex') {
            let credits = raw.credits || {};
            if (credits.unlimited)
                return 'Credits: unlimited';
            let balance = parseFloat(credits.balance);
            if (_isNumber(balance) && balance > 0)
                return `Credits: $${balance.toFixed(2)}`;
            return '';
        }

        if (_isNumber(metrics.cost)) {
            if (_isNumber(metrics.limit) && providerName === 'openrouter')
                return `$${metrics.cost.toFixed(4)} / $${metrics.limit.toFixed(2)}`;
            return `$${metrics.cost.toFixed(4)}`;
        }

        /* Percentage-based quotas must never be printed as "credits". */
        if (PERCENT_QUOTA_PROVIDERS.includes(providerName))
            return '';

        if (_isNumber(metrics.remaining) && _isNumber(metrics.limit))
            return `${metrics.remaining.toFixed(1)} / ${metrics.limit.toFixed(1)} credits`;

        return '';
    }

    _byokText(providerName, data) {
        if (providerName !== 'openrouter')
            return '';

        const payload = (data.raw || {}).data;
        if (!payload)
            return '';

        let byokValue;
        if (data.window === '5h')
            byokValue = payload.byok_usage_daily;
        else if (data.window === '30d')
            byokValue = payload.byok_usage_monthly;
        else
            byokValue = payload.byok_usage_weekly;

        if (_isNumber(byokValue) && byokValue > 0)
            return `BYOK: $${byokValue.toFixed(4)}`;
        return '';
    }

    /* ------------------------------------------------------------------
     * Data
     * ------------------------------------------------------------------ */

    _startAutoRefresh() {
        if (this._timeout)
            GLib.source_remove(this._timeout);

        this._timeout = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT,
            this._refreshSeconds,
            () => {
                this._refreshData();
                return GLib.SOURCE_CONTINUE;
            }
        );
    }

    /* Two watchers with different consequences: display options only need a
     * re-render, while a changed credential means the CLI has something new to
     * say and is worth an actual fetch. */
    _watchConfigFiles() {
        try {
            GLib.mkdir_with_parents(CONFIG_DIR, 0o755);

            this._settingsMonitor = Gio.File.new_for_path(SETTINGS_PATH)
                .monitor_file(Gio.FileMonitorFlags.NONE, null);
            this._settingsMonitor.connect('changed', () => {
                this._onSettingsFileChanged();
            });
        } catch (_e) {
            this._settingsMonitor = null;
        }

        try {
            this._envMonitor = Gio.File.new_for_path(ENV_FILE_PATH)
                .monitor_file(Gio.FileMonitorFlags.NONE, null);
            this._envMonitor.connect('changed', () => {
                this._refreshData();
            });
        } catch (_e) {
            this._envMonitor = null;
        }
    }

    /* Re-renders from cached data. Never refetches: changing a display option
     * must not cost a CLI call or blank the panel. */
    _onSettingsFileChanged() {
        this._settings = _loadSettings();

        let provider = this._settings.panelProvider || 'claude';
        if (provider !== this._panelProvider) {
            this._panelProvider = provider;
            this._updateProviderBadge();
        }

        this._panelWindows = this._sanitizePanelWindows(this._settings.panelWindows);
        this._providerOrder = this._sanitizeProviderOrder(this._settings.providerOrder);
        this._visibleProviders = this._sanitizeVisibleProviders(this._settings.visibleProviders);

        let seconds = this._sanitizeRefreshSeconds(this._settings.refreshSeconds);
        if (seconds !== this._refreshSeconds) {
            this._refreshSeconds = seconds;
            this._startAutoRefresh();
        }

        this._updateUI();
    }

    _refreshData() {
        this._panelLabel.set_text('...');

        try {
            let launcher = new Gio.SubprocessLauncher({
                flags: Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
            });

            let envFromFile = _loadEnvFromFile();
            for (let [key, value] of Object.entries(envFromFile))
                launcher.setenv(key, value, true);

            let proc = launcher.spawnv([_getCliPath(), 'show', '--json']);

            proc.communicate_utf8_async(null, null, (proc, result) => {
                try {
                    let [ok, stdout, stderr] = proc.communicate_utf8_finish(result);

                    if (ok && proc.get_successful() && stdout) {
                        if (this._parseOutput(stdout.trim()))
                            this._updateUI();
                    } else {
                        this._handleError(stderr || 'Command failed');
                    }
                } catch (e) {
                    this._handleError(e.message);
                }
            });
        } catch (e) {
            this._handleError(e.message);
        }
    }

    _parseOutput(output) {
        try {
            let json = JSON.parse(output);
            this._usageData = json;
            this._lastUpdated = new Date();
            return true;
        } catch (e) {
            console.log(`ai-usage: JSON parse error: ${e.message}`);
            this._handleError(`Parse error: ${e.message}`);
            return false;
        }
    }

    _updateUI() {
        const entries = Object.entries(this._usageData).sort((a, b) => {
            const aIndex = this._providerOrder.indexOf(a[0]);
            const bIndex = this._providerOrder.indexOf(b[0]);
            const aRank = aIndex === -1 ? 999 : aIndex;
            const bRank = bIndex === -1 ? 999 : bIndex;
            if (aRank !== bRank)
                return aRank - bRank;
            return a[0].localeCompare(b[0]);
        });

        for (let [providerName, data] of entries) {
            /* Don't even build a card for a provider the user turned off. */
            if (!this._isProviderVisible(providerName))
                continue;
            this._updateProviderCard(providerName, data);
        }

        this._applyProviderVisibility();
        this._applyProviderOrder();
        this._updatePanelLabel();

        if (this._lastUpdated) {
            let timeString = this._lastUpdated.toLocaleTimeString('en-US', {
                hour: '2-digit',
                minute: '2-digit',
            });
            this._lastUpdatedItem.label.set_text(`Last updated: ${timeString}`);
        }
    }

    /* A card is shown when the provider is enabled and the CLI reported it. */
    _applyProviderVisibility() {
        for (let [providerName, card] of Object.entries(this._providerRows)) {
            let show = this._isProviderVisible(providerName)
                && !!this._usageData[providerName];

            if (show) {
                /* It may have been built before being switched off, so make sure
                 * its data is current before showing it again. */
                if (!card.container.visible)
                    this._populateProviderCard(card, providerName, this._usageData[providerName]);
                card.container.show();
            } else {
                card.container.hide();
            }
        }
    }

    /* Cards are created in whatever order the data arrived, so restack them to
     * match the configured order. */
    _applyProviderOrder() {
        let index = 0;
        for (let providerName of this._providerOrder) {
            let card = this._providerRows[providerName];
            if (!card)
                continue;
            this._providersContainer.set_child_at_index(card.container, index);
            index++;
        }
    }

    _updatePanelLabel() {
        this._panelLabel.set_text(this._panelText());
    }

    /* Percentage providers show the configured window(s); a provider that only
     * has one window shows that one, and a money-based provider shows the
     * amount spent instead of a percentage. */
    _panelText() {
        let data = this._usageData[this._panelProvider];
        if (!data || data.error)
            return 'N/A';

        const metrics = data.metrics || {};

        if (MONEY_PROVIDERS.includes(this._panelProvider) && _isNumber(metrics.cost))
            return `$${metrics.cost.toFixed(2)}`;

        let windows = this._extractWindows(this._panelProvider, data);
        if (windows.length === 0)
            return 'N/A';

        let wanted = this._panelWindows === 'both' ? ['5h', '7d'] : [this._panelWindows];
        let picks = windows.filter(w => wanted.includes(w.key));

        /* Requested window missing (Codex has no 5h): fall back to what exists. */
        if (picks.length === 0)
            picks = windows.slice(0, wanted.length);

        return picks.map(w => `${w.pct.toFixed(0)}%`).join(' · ');
    }

    _handleError(message) {
        console.log(`ai-usage error: ${message}`);
        this._panelLabel.set_text('Err');
        this._lastUpdatedItem.label.set_text(`Error: ${String(message).substring(0, 40)}`);
    }

    destroy() {
        if (this._timeout) {
            GLib.source_remove(this._timeout);
            this._timeout = null;
        }

        for (let name of ['_settingsMonitor', '_envMonitor']) {
            if (this[name]) {
                this[name].cancel();
                this[name] = null;
            }
        }


        super.destroy();
    }
});

class AiUsageExtension {
    constructor() {
        this._indicator = null;
    }

    enable() {
        this._indicator = new UsageIndicator();
        Main.panel.addToStatusArea('ai-usage', this._indicator, 0, 'right');
    }

    disable() {
        if (this._indicator) {
            this._indicator.destroy();
            this._indicator = null;
        }
    }
}

function init() {
    return new AiUsageExtension();
}
