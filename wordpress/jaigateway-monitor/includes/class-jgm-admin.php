<?php
if (!defined('ABSPATH')) {
    exit;
}

/** The settings page (Settings → JaiGateway Monitor) and the "Send a test error" button. */
class JGM_Admin
{
    const PAGE = 'jaigateway-monitor';
    const GROUP = 'jgm_group';

    public static function boot(): void
    {
        add_action('admin_menu', [self::class, 'menu']);
        add_action('admin_init', [self::class, 'register']);
        add_action('admin_post_jgm_test', [self::class, 'handle_test']);
        add_filter('plugin_action_links_' . plugin_basename(JGM_FILE), [self::class, 'action_links']);
        add_action('admin_notices', [self::class, 'notice_if_unconfigured']);
    }

    public static function menu(): void
    {
        add_options_page('JaiGateway Monitor', 'JaiGateway Monitor', 'manage_options', self::PAGE, [self::class, 'render']);
    }

    public static function action_links(array $links): array
    {
        array_unshift($links, '<a href="' . esc_url(admin_url('options-general.php?page=' . self::PAGE)) . '">Settings</a>');

        return $links;
    }

    public static function notice_if_unconfigured(): void
    {
        if (!current_user_can('manage_options') || JGM_Plugin::is_configured()) {
            return;
        }
        $screen = function_exists('get_current_screen') ? get_current_screen() : null;
        if ($screen && $screen->id === 'settings_page_' . self::PAGE) {
            return;
        }
        echo '<div class="notice notice-info"><p><strong>JaiGateway Monitor</strong> is installed but not connected yet. <a href="' . esc_url(admin_url('options-general.php?page=' . self::PAGE)) . '">Add your address and API key</a>.</p></div>';
    }

    public static function register(): void
    {
        register_setting(self::GROUP, JGM_Plugin::OPTION, ['type' => 'array', 'sanitize_callback' => [self::class, 'sanitize'], 'default' => JGM_Plugin::defaults()]);
    }

    /** @param mixed $in */
    public static function sanitize($in): array
    {
        $in = is_array($in) ? $in : [];
        $old = get_option(JGM_Plugin::OPTION, []);
        $old = is_array($old) ? $old : [];
        $out = JGM_Plugin::defaults();

        $host = untrailingslashit(trim((string) ($in['host'] ?? '')));
        $out['host'] = (preg_match('#^https?://[^\s/]+#i', $host) === 1) ? esc_url_raw($host) : '';
        if ($host !== '' && $out['host'] === '') {
            add_settings_error(JGM_Plugin::OPTION, 'jgm_host', 'The address must start with https:// (for example https://monitor.jaigateway.com).');
        }
        // an empty key field keeps the saved key (the field never shows it back)
        $key = trim((string) ($in['api_key'] ?? ''));
        $out['api_key'] = $key !== '' ? preg_replace('/[^A-Za-z0-9_\-]/', '', $key) : (string) ($old['api_key'] ?? '');
        if (!empty($in['clear_key'])) {
            $out['api_key'] = '';
        }
        $out['environment'] = substr(preg_replace('/[^A-Za-z0-9_\-.]/', '', (string) ($in['environment'] ?? 'production')), 0, 40);
        $out['release'] = substr(preg_replace('/[^A-Za-z0-9_\-.@+]/', '', (string) ($in['release'] ?? '')), 0, 80);
        foreach (['report_php', 'report_js', 'js_admin', 'web_vitals', 'performance', 'mail_failures', 'send_user'] as $flag) {
            $out[$flag] = empty($in[$flag]) ? 0 : 1;
        }
        $out['vitals_sample'] = max(1, min(100, (int) ($in['vitals_sample'] ?? 10)));
        $ping = trim((string) ($in['cron_ping_url'] ?? ''));
        $out['cron_ping_url'] = '';
        if ($ping !== '') {
            if (JGM_Plugin::valid_ping_url($ping, array_merge($out, ['host' => $out['host'] ?: (string) ($old['host'] ?? '')]))) {
                $out['cron_ping_url'] = esc_url_raw($ping);
            } else {
                add_settings_error(JGM_Plugin::OPTION, 'jgm_ping', 'The cron ping address must be on the same server as above and look like https://…/api/ping/<token>/. It was not saved.');
            }
        }

        return $out;
    }

    // ----------------------------------------------------------------------------------------------- test button

    public static function handle_test(): void
    {
        if (!current_user_can('manage_options')) {
            wp_die('Not allowed', 403);
        }
        check_admin_referer('jgm_test');
        $s = JGM_Plugin::settings();
        $back = admin_url('options-general.php?page=' . self::PAGE);
        if (!JGM_Plugin::is_configured($s)) {
            set_transient('jgm_test_result', ['ok' => false, 'msg' => 'Save your address and API key first.'], 60);
            wp_safe_redirect($back);
            exit;
        }
        $res = wp_remote_post($s['host'] . '/api/ingest/events/', [
            'timeout' => 8,
            'headers' => ['Content-Type' => 'application/json', 'X-API-Key' => $s['api_key']],
            'body' => wp_json_encode([
                'event_type' => 'error',
                'description' => 'JaiGateway Monitor test from WordPress',
                'level' => 'info',
                'platform' => 'php',
                'environment' => $s['environment'] ?: null,
                'tags' => ['test' => 'yes', 'wordpress' => get_bloginfo('version'), 'plugin_version' => JGM_VERSION],
                'exception' => ['type' => 'TestEvent', 'value' => 'If you can see this in your dashboard, the connection works.', 'stacktrace' => [['filename' => 'wp-content/plugins/jaigateway-monitor/test.php', 'function' => 'jgm_test', 'lineno' => 1, 'in_app' => true]]],
            ]),
        ]);
        set_transient('jgm_test_result', self::explain($res), 60);
        wp_safe_redirect($back);
        exit;
    }

    /** @param array|\WP_Error $res */
    public static function explain($res): array
    {
        if (is_wp_error($res)) {
            return ['ok' => false, 'msg' => 'Could not reach the server: ' . $res->get_error_message() . '. Check the address, and that this site can make outgoing HTTPS requests.'];
        }
        $code = (int) wp_remote_retrieve_response_code($res);
        if ($code >= 200 && $code < 300) {
            return ['ok' => true, 'msg' => 'Connected. A test error was sent: open Errors in your JaiGateway Monitor dashboard to see "JaiGateway Monitor test from WordPress".'];
        }
        $map = [
            401 => 'The API key was not recognised. Copy it again from API Keys in your dashboard.',
            403 => 'This key cannot send data. Create a key with the Write permission (a Read-only key is for reading data).',
            404 => 'Nothing answered at that address. Use your dashboard address, for example https://monitor.jaigateway.com (no extra path).',
            429 => 'The key is over its limit or your monthly allowance is used up. Try again later or check your plan.',
        ];

        return ['ok' => false, 'msg' => ($map[$code] ?? 'The server answered with an error.') . ' (HTTP ' . $code . ')'];
    }

    // ---------------------------------------------------------------------------------------------------- page

    public static function render(): void
    {
        if (!current_user_can('manage_options')) {
            return;
        }
        $s = JGM_Plugin::settings();
        $o = JGM_Plugin::OPTION;
        $locked_host = defined('JGM_HOST') && JGM_HOST;
        $locked_key = defined('JGM_API_KEY') && JGM_API_KEY;
        $hint = $s['api_key'] !== '' ? '••••' . substr($s['api_key'], -4) : '';
        $test = get_transient('jgm_test_result');
        if ($test) {
            delete_transient('jgm_test_result');
        }
        $cb = static function (string $name, string $label, string $help = '') use ($s, $o) {
            echo '<tr><th scope="row">' . esc_html($label) . '</th><td><label><input type="checkbox" name="' . esc_attr($o . '[' . $name . ']') . '" value="1" ' . checked(!empty($s[$name]), true, false) . '> ' . esc_html($help) . '</label></td></tr>';
        };
        ?>
        <div class="wrap">
            <h1>JaiGateway Monitor</h1>
            <p>Reports errors, WP-Cron problems and page speed from this site to your JaiGateway Monitor account.
                <a href="https://monitor.jaigateway.com/docs/wordpress/" target="_blank" rel="noopener noreferrer">Setup guide</a></p>

            <?php if ($test) : ?>
                <div class="notice <?php echo $test['ok'] ? 'notice-success' : 'notice-error'; ?>"><p><?php echo esc_html($test['msg']); ?></p></div>
            <?php endif; ?>
            <?php settings_errors($o); ?>

            <form method="post" action="options.php">
                <?php settings_fields(self::GROUP); ?>
                <h2>Connection</h2>
                <table class="form-table" role="presentation">
                    <tr><th scope="row"><label for="jgm_host">Server address</label></th><td>
                        <input id="jgm_host" type="url" class="regular-text" name="<?php echo esc_attr($o); ?>[host]" value="<?php echo esc_attr($s['host']); ?>" placeholder="https://monitor.jaigateway.com" <?php disabled($locked_host); ?>>
                        <?php if ($locked_host) : ?><p class="description">Set in wp-config.php (JGM_HOST).</p><?php endif; ?></td></tr>
                    <tr><th scope="row"><label for="jgm_key">API key</label></th><td>
                        <input id="jgm_key" type="password" class="regular-text" autocomplete="new-password" name="<?php echo esc_attr($o); ?>[api_key]" value="" placeholder="<?php echo esc_attr($hint !== '' ? $hint . ' (saved; leave empty to keep it)' : 'ak_…'); ?>" <?php disabled($locked_key); ?>>
                        <?php if ($locked_key) : ?>
                            <p class="description">Set in wp-config.php (JGM_API_KEY).</p>
                        <?php else : ?>
                            <p class="description">Create it under <strong>API Keys</strong> in your dashboard with the <strong>Write</strong> permission, one key per site. The browser part of this plugin uses the same key, which is normal for a write-only key.
                                To keep it out of the database, define <code>JGM_API_KEY</code> in wp-config.php instead.</p>
                            <?php if ($hint !== '') : ?><label><input type="checkbox" name="<?php echo esc_attr($o); ?>[clear_key]" value="1"> Remove the saved key</label><?php endif; ?>
                        <?php endif; ?></td></tr>
                    <tr><th scope="row"><label for="jgm_env">Environment</label></th><td>
                        <input id="jgm_env" type="text" class="regular-text" name="<?php echo esc_attr($o); ?>[environment]" value="<?php echo esc_attr($s['environment']); ?>" placeholder="production">
                        <p class="description">For example production or staging. Lets you alert on production only.</p></td></tr>
                    <tr><th scope="row"><label for="jgm_rel">Release (optional)</label></th><td>
                        <input id="jgm_rel" type="text" class="regular-text" name="<?php echo esc_attr($o); ?>[release]" value="<?php echo esc_attr($s['release']); ?>" placeholder="shop@1.4.2"></td></tr>
                </table>

                <h2>What to report</h2>
                <table class="form-table" role="presentation">
                    <?php
                    $cb('report_php', 'PHP errors', 'Fatal errors and exceptions, with the plugin or theme that caused them');
                    $cb('mail_failures', 'Email failures', 'When WordPress cannot send an email (addresses are never included)');
                    $cb('report_js', 'JavaScript errors', 'Errors your visitors hit in their browsers');
                    $cb('js_admin', 'JavaScript errors in wp-admin', 'Also watch the admin area');
                    $cb('web_vitals', 'Page speed', 'Measure how fast pages load for real visitors (Core Web Vitals)');
                    ?>
                    <tr><th scope="row"><label for="jgm_vs">Share of visits measured</label></th><td>
                        <input id="jgm_vs" type="number" min="1" max="100" name="<?php echo esc_attr($o); ?>[vitals_sample]" value="<?php echo esc_attr((string) $s['vitals_sample']); ?>" class="small-text"> %
                        <p class="description">Only used with Page speed. 10% is plenty for most sites.</p></td></tr>
                    <?php
                    $cb('performance', 'Server performance', 'Time every request on the server (front pages, admin, AJAX, REST) by page type, plus the database queries and outside calls inside it. Adds a little work to each request, so it is off by default');
                    $cb('send_user', 'Which user', 'Include the WordPress user id (never the name or email) with errors');
                    ?>
                </table>

                <h2>WP-Cron</h2>
                <table class="form-table" role="presentation">
                    <tr><th scope="row"><label for="jgm_ping">Cron job ping address</label></th><td>
                        <input id="jgm_ping" type="url" class="large-text" name="<?php echo esc_attr($o); ?>[cron_ping_url]" value="<?php echo esc_attr($s['cron_ping_url']); ?>" placeholder="https://monitor.jaigateway.com/api/ping/…/">
                        <p class="description">Optional but recommended. In your dashboard create a <strong>Cron job</strong> that runs every 1 hour with a 15 minute grace period, and paste its ping address here.
                            This site then pings it every hour from WP-Cron, so you are alerted if WP-Cron stops (scheduled posts, backups and shop jobs silently stop with it).</p></td></tr>
                </table>
                <?php submit_button(); ?>
            </form>

            <hr>
            <h2>Test the connection</h2>
            <form method="post" action="<?php echo esc_url(admin_url('admin-post.php')); ?>">
                <input type="hidden" name="action" value="jgm_test">
                <?php wp_nonce_field('jgm_test'); ?>
                <?php submit_button('Send a test error', 'secondary', 'submit', false); ?>
                <p class="description">Save your settings first. The test shows up as an info-level event in your dashboard.</p>
            </form>
        </div>
        <?php
    }
}
