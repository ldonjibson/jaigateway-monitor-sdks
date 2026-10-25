<?php
if (!defined('ABSPATH')) {
    exit;
}

/**
 * Settings, the PHP error reporting (through the bundled SDK), the front-end script and the WP-Cron heartbeat.
 * Everything here is passive: a failure in this plugin must never break the site it watches.
 */
class JGM_Plugin
{
    const OPTION = 'jgm_settings';
    const CRON_HOOK = 'jgm_cron_ping';

    /** @return array<string,mixed> */
    public static function defaults(): array
    {
        return [
            'host' => '',
            'api_key' => '',
            'environment' => 'production',
            'release' => '',
            'report_php' => 1,
            'report_js' => 1,
            'js_admin' => 0,
            'web_vitals' => 0,
            'vitals_sample' => 10,
            'performance' => 0,
            'mail_failures' => 1,
            'send_user' => 0,
            'cron_ping_url' => '',
        ];
    }

    /** Settings with wp-config.php constants (JGM_HOST, JGM_API_KEY) taking priority, so the key can stay out of the database. */
    public static function settings(): array
    {
        $saved = get_option(self::OPTION, []);
        $s = array_merge(self::defaults(), is_array($saved) ? $saved : []);
        if (defined('JGM_HOST') && JGM_HOST) {
            $s['host'] = (string) JGM_HOST;
        }
        if (defined('JGM_API_KEY') && JGM_API_KEY) {
            $s['api_key'] = (string) JGM_API_KEY;
        }
        $s['host'] = untrailingslashit(trim((string) $s['host']));

        return $s;
    }

    public static function is_configured(?array $s = null): bool
    {
        $s = $s ?? self::settings();

        return $s['api_key'] !== '' && preg_match('#^https?://#i', $s['host']) === 1;
    }

    public static function boot(): void
    {
        add_action('plugins_loaded', [self::class, 'init'], 0);
        add_action(self::CRON_HOOK, [self::class, 'cron_ping']);
    }

    public static function init(): void
    {
        $s = self::settings();
        if (!self::is_configured($s)) {
            return;
        }

        if ($s['report_php'] && class_exists('Insight\\Insight')) {
            try {
                self::init_sdk($s);
            } catch (\Throwable $e) {
                // never break the site
            }
        }
        if ($s['report_js']) {
            add_action('wp_enqueue_scripts', [self::class, 'enqueue_frontend'], 1);
            if ($s['js_admin']) {
                add_action('admin_enqueue_scripts', [self::class, 'enqueue_frontend'], 1);
            }
        }
        if ($s['mail_failures']) {
            add_action('wp_mail_failed', [self::class, 'on_mail_failed']);
        }
        self::sync_cron($s);
    }

    // ------------------------------------------------------------------------------------------ PHP errors

    private static function init_sdk(array $s): void
    {
        global $wp_version;
        $host = wp_parse_url(home_url(), PHP_URL_HOST);
        \Insight\Insight::init([
            'api_key' => $s['api_key'],
            'host' => $s['host'],
            'environment' => $s['environment'] !== '' ? $s['environment'] : null,
            'release' => $s['release'] !== '' ? $s['release'] : null,
            'server_name' => is_string($host) ? $host : null,
            'send_default_pii' => false,
            'timeout' => 2,
            'connect_timeout' => 1,
            'performance' => (bool) $s['performance'],
            'before_send' => [self::class, 'before_send'],
        ]);
        \Insight\Insight::setTags([
            'wordpress' => (string) $wp_version,
            'multisite' => is_multisite() ? 'yes' : 'no',
            'theme' => (string) get_stylesheet(),
            'locale' => (string) get_locale(),
            'plugin_version' => JGM_VERSION,
        ]);
        \Insight\Insight::registerErrorHandlers();

        if ($s['performance']) {
            \Insight\Insight::startTransaction();
            $script = isset($GLOBALS['pagenow']) ? (string) $GLOBALS['pagenow'] : '';
            if ($script === 'wp-login.php') {
                \Insight\Insight::setTransactionName('/wp-login');
            } elseif (defined('DOING_CRON') && DOING_CRON) {
                \Insight\Insight::setTransactionName('/wp-cron');
            }
            add_action('wp', [self::class, 'name_front_request'], 99);
            add_action('admin_init', [self::class, 'name_admin_request'], 99);
            add_action('rest_api_init', [self::class, 'name_rest_request'], 99);
            self::init_spans();
        }
        if ($s['send_user']) {
            add_action('init', static function () {
                $id = get_current_user_id();
                if ($id > 0) {
                    \Insight\Insight::setUser(['id' => (string) $id]); // the id only: no name, no email
                }
            }, 20);
        }
    }

    // ---------------------------------------------------------------------------- spans: database and outside calls

    /** @var array<int,array{0:float,1:string}> */
    private static array $http_pending = [];

    /**
     * Queries come from $wpdb->queries (WordPress keeps them, with timings, when SAVEQUERIES is on) and outside calls from the
     * WordPress HTTP API hooks. Both only run with "Server response times" on; the SQL has its values removed before it is sent.
     */
    private static function init_spans(): void
    {
        if (!defined('SAVEQUERIES')) {
            define('SAVEQUERIES', true);
        }
        add_filter('pre_http_request', [self::class, 'http_start'], 1, 3);
        add_action('http_api_debug', [self::class, 'http_end'], 10, 5);
        add_action('shutdown', [self::class, 'flush_db_spans'], 0);
    }

    /** @param mixed $pre */
    public static function http_start($pre, $args = null, $url = '')
    {
        if (count(self::$http_pending) < 50) {
            self::$http_pending[] = [microtime(true), (string) $url];
        }

        return $pre;
    }

    /** @param mixed $response */
    public static function http_end($response, $context = '', $class = '', $args = [], $url = ''): void
    {
        if ($context !== 'response') {
            return;
        }
        try {
            $start = microtime(true);
            foreach (array_reverse(array_keys(self::$http_pending)) as $i) {
                if (self::$http_pending[$i][1] === (string) $url) {
                    $start = self::$http_pending[$i][0];
                    unset(self::$http_pending[$i]);
                    break;
                }
            }
            $parts = wp_parse_url((string) $url);
            $host = is_array($parts) && !empty($parts['host']) ? strtolower($parts['host']) . (!empty($parts['port']) ? ':' . $parts['port'] : '') : '';
            $monitor = wp_parse_url(self::settings()['host']);
            if ($host === '' || (is_array($monitor) && !empty($monitor['host']) && strtolower($monitor['host']) === strtolower((string) ($parts['host'] ?? '')))) {
                return; // the monitor's own traffic (cron ping, connection test) is not the site's
            }
            $method = is_array($args) && !empty($args['method']) ? strtoupper((string) $args['method']) : 'GET';
            $code = is_wp_error($response) ? null : (int) wp_remote_retrieve_response_code($response);
            $ok = $code !== null && $code > 0 && $code < 500;
            $data = ['http.method' => $method, 'host' => $host];
            if ($code) {
                $data['http.status_code'] = $code;
            }
            \Insight\Insight::recordSpan('http.client', $method . ' ' . $host, (microtime(true) - $start) * 1000, $ok, $data, $start);
        } catch (\Throwable $e) {
            // never break the site
        }
    }

    /** At shutdown: every query WordPress ran this request, with its time (at most 400 per request). */
    public static function flush_db_spans(): void
    {
        try {
            global $wpdb;
            if (!isset($wpdb->queries) || !is_array($wpdb->queries) || !class_exists('Insight\\Insight')) {
                return;
            }
            $n = 0;
            foreach ($wpdb->queries as $q) {
                if (!is_array($q) || empty($q[0]) || !isset($q[1]) || ++$n > 400) {
                    continue;
                }
                $ms = ((float) $q[1]) * 1000;
                $start = isset($q[3]) && is_numeric($q[3]) ? (float) $q[3] : null;
                \Insight\Insight::recordSpan('db.query', (string) $q[0], $ms, true, ['db.system' => 'mysql', 'db.name' => (string) $wpdb->dbname], $start);
            }
        } catch (\Throwable $e) {
            // never break the site
        }
    }

    /** Where in wp-content an error came from: "plugin:woocommerce", "theme:astra", "mu-plugin:x", "core", or "other". */
    public static function culprit_for_path(string $file): string
    {
        $file = wp_normalize_path($file);
        $roots = [
            'plugin' => wp_normalize_path(WP_PLUGIN_DIR) . '/',
            'mu-plugin' => wp_normalize_path(defined('WPMU_PLUGIN_DIR') ? WPMU_PLUGIN_DIR : WP_CONTENT_DIR . '/mu-plugins') . '/',
            'theme' => wp_normalize_path(get_theme_root()) . '/',
        ];
        foreach ($roots as $kind => $root) {
            if (strpos($file, $root) === 0) {
                $slug = explode('/', substr($file, strlen($root)), 2)[0];
                $slug = preg_replace('/\.php$/', '', $slug);

                return $kind . ':' . $slug;
            }
        }
        $abs = wp_normalize_path(ABSPATH);
        if (strpos($file, $abs . 'wp-includes/') === 0 || strpos($file, $abs . 'wp-admin/') === 0) {
            return 'core';
        }

        return 'other';
    }

    /** The first (most recent) frame that is in a plugin or theme names the culprit; WordPress core only if nothing else is. */
    public static function culprit_for_frames(array $frames): string
    {
        $core = false;
        foreach ($frames as $f) {
            if (!is_array($f) || empty($f['filename'])) {
                continue;
            }
            $c = self::culprit_for_path((string) $f['filename']);
            if ($c === 'core') {
                $core = true;
                continue;
            }
            if ($c !== 'other') {
                return $c;
            }
        }

        return $core ? 'core' : 'other';
    }

    /** SDK hook: tag the culprit and give the same bug the same group wherever the site lives. */
    public static function before_send($event)
    {
        try {
            if (!is_array($event)) {
                return $event;
            }
            $frames = isset($event['exception']['stacktrace']) && is_array($event['exception']['stacktrace']) ? $event['exception']['stacktrace'] : [];
            if ($frames) {
                $culprit = self::culprit_for_frames($frames);
                $event['tags']['culprit'] = $culprit;
                if (strpos($culprit, ':') !== false) {
                    [$kind, $slug] = explode(':', $culprit, 2);
                    $event['tags'][$kind] = $slug;
                    if ($kind === 'plugin' && function_exists('get_plugins')) {
                        // nothing else: reading plugin headers on every error would be slow
                    }
                }
            }
            $event['extra']['active_plugins'] = self::active_plugin_slugs();
        } catch (\Throwable $e) {
            // keep the event as it is
        }

        return $event;
    }

    /** @return string[] slugs only (folder names), capped */
    public static function active_plugin_slugs(): array
    {
        $out = [];
        foreach ((array) get_option('active_plugins', []) as $p) {
            $out[] = explode('/', (string) $p)[0];
            if (count($out) >= 80) {
                break;
            }
        }

        return $out;
    }

    // ------------------------------------------------------------------------------- request names (performance)

    public static function front_route(): string
    {
        if (is_404()) {
            return '/404';
        }
        if (is_search()) {
            return '/search';
        }
        if (is_front_page() || is_home()) {
            return '/';
        }
        if (is_singular()) {
            return '/' . (get_post_type() ?: 'post') . '/{slug}';
        }
        if (is_category() || is_tag() || is_tax()) {
            $q = get_queried_object();

            return '/' . (is_object($q) && !empty($q->taxonomy) ? $q->taxonomy : 'archive') . '/{slug}';
        }
        if (is_author()) {
            return '/author/{slug}';
        }
        if (is_date()) {
            return '/archive/date';
        }
        if (is_archive()) {
            return '/archive';
        }

        return '/page';
    }

    public static function name_front_request(): void
    {
        if (is_admin() || wp_doing_cron()) {
            return;
        }
        \Insight\Insight::setTransactionName(self::front_route());
    }

    public static function name_admin_request(): void
    {
        if (wp_doing_ajax()) {
            $action = isset($_REQUEST['action']) ? preg_replace('/[^a-z0-9_\-]/i', '', (string) wp_unslash($_REQUEST['action'])) : 'unknown'; // phpcs:ignore WordPress.Security.NonceVerification
            \Insight\Insight::setTransactionName('/admin-ajax/' . $action);

            return;
        }
        $page = isset($_GET['page']) ? preg_replace('/[^a-z0-9_\-]/i', '', (string) wp_unslash($_GET['page'])) : ''; // phpcs:ignore WordPress.Security.NonceVerification
        $script = isset($GLOBALS['pagenow']) ? (string) $GLOBALS['pagenow'] : 'admin';
        \Insight\Insight::setTransactionName('/wp-admin/' . ($page !== '' ? $page : $script));
    }

    public static function name_rest_request(): void
    {
        $route = isset($GLOBALS['wp']->query_vars['rest_route']) ? (string) $GLOBALS['wp']->query_vars['rest_route'] : '';
        $route = preg_replace('#/\d+#', '/{id}', $route);
        \Insight\Insight::setTransactionName('/wp-json' . $route);
    }

    // ----------------------------------------------------------------------------------------- wp_mail failures

    public static function on_mail_failed($error): void
    {
        try {
            if (!class_exists('Insight\\Insight') || !($error instanceof \WP_Error)) {
                return;
            }
            $message = $error->get_error_message();
            $message = preg_replace('/[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}/i', '[email]', (string) $message); // no recipient addresses
            \Insight\Insight::captureMessage('wp_mail failed: ' . $message, 'warning', ['tags' => ['culprit' => 'mail']]);
        } catch (\Throwable $e) {
            // never break the site
        }
    }

    // ----------------------------------------------------------------------------------------- front-end script

    public static function enqueue_frontend(): void
    {
        $s = self::settings();
        if (!self::is_configured($s) || !$s['report_js']) {
            return;
        }
        $deps = [];
        $init = [
            'apiKey' => $s['api_key'],
            'host' => $s['host'],
            'environment' => $s['environment'] !== '' ? $s['environment'] : null,
            'release' => $s['release'] !== '' ? $s['release'] : null,
        ];
        if ($s['web_vitals'] && !is_admin()) {
            wp_register_script('jgm-tracing', plugins_url('assets/insight.tracing.min.js', JGM_FILE), [], JGM_VERSION, false);
            $deps[] = 'jgm-tracing'; // must be present before the SDK starts
            $init['tracing'] = ['sampleRate' => max(0.01, min(1.0, ((int) $s['vitals_sample']) / 100))];
        }
        wp_register_script('jgm-sdk', plugins_url('assets/insight.min.js', JGM_FILE), $deps, JGM_VERSION, false);
        wp_enqueue_script('jgm-sdk');
        $route = (!is_admin() && $s['web_vitals']) ? 'Insight.setRoute(' . wp_json_encode(self::front_route()) . ');' : '';
        wp_add_inline_script('jgm-sdk', $route . 'Insight.init(' . wp_json_encode(array_filter($init, static fn ($v) => $v !== null)) . ');', 'after');
    }

    // ---------------------------------------------------------------------------------------------- WP-Cron

    /** Schedules (or removes) the hourly ping that proves WP-Cron is running. */
    private static function sync_cron(array $s): void
    {
        $url = trim((string) $s['cron_ping_url']);
        $next = wp_next_scheduled(self::CRON_HOOK);
        if ($url !== '' && self::valid_ping_url($url, $s)) {
            if (!$next) {
                wp_schedule_event(time() + 60, 'hourly', self::CRON_HOOK);
            }
        } elseif ($next) {
            wp_unschedule_event($next, self::CRON_HOOK);
        }
    }

    /** Only http(s) addresses on the configured monitor host, under /api/ping/. */
    public static function valid_ping_url(string $url, ?array $s = null): bool
    {
        $s = $s ?? self::settings();
        $u = wp_parse_url($url);
        $h = wp_parse_url($s['host']);
        if (!is_array($u) || !is_array($h) || empty($u['host']) || empty($h['host'])) {
            return false;
        }

        return in_array(($u['scheme'] ?? ''), ['http', 'https'], true)
            && strtolower($u['host']) === strtolower($h['host'])
            && strpos($u['path'] ?? '', '/api/ping/') === 0;
    }

    public static function cron_ping(): void
    {
        $s = self::settings();
        $url = trim((string) $s['cron_ping_url']);
        if ($url === '' || !self::valid_ping_url($url, $s)) {
            return;
        }
        wp_remote_get(trailingslashit($url), ['timeout' => 5, 'redirection' => 2, 'user-agent' => 'JaiGatewayMonitor-WordPress/' . JGM_VERSION]);
    }

    public static function deactivate(): void
    {
        $next = wp_next_scheduled(self::CRON_HOOK);
        if ($next) {
            wp_unschedule_event($next, self::CRON_HOOK);
        }
    }
}
