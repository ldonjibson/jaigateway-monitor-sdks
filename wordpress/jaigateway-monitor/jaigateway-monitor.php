<?php
/**
 * Plugin Name:       JaiGateway Monitor
 * Plugin URI:        https://monitor.jaigateway.com/docs/wordpress/
 * Description:       Know when your WordPress site breaks: PHP errors with the plugin or theme that caused them, JavaScript errors, WP-Cron that stopped running, and page speed. Reports to your JaiGateway Monitor account.
 * Version:           1.0.0
 * Requires at least: 5.8
 * Requires PHP:      8.0
 * Author:            JaiGateway
 * Author URI:        https://jaigateway.com
 * License:           MIT
 * License URI:       https://opensource.org/licenses/MIT
 * Text Domain:       jaigateway-monitor
 */

if (!defined('ABSPATH')) {
    exit;
}

define('JGM_VERSION', '1.0.0');
define('JGM_FILE', __FILE__);
define('JGM_DIR', plugin_dir_path(__FILE__));

// The bundled PHP SDK (namespace Insight\) is loaded on demand; nothing is needed from Composer.
spl_autoload_register(static function ($class) {
    if (strpos($class, 'Insight\\') === 0) {
        $file = JGM_DIR . 'lib/Insight/' . str_replace('\\', '/', substr($class, 8)) . '.php';
        if (is_readable($file)) {
            require $file;
        }
    }
});

require_once JGM_DIR . 'includes/class-jgm-plugin.php';
require_once JGM_DIR . 'includes/class-jgm-admin.php';

JGM_Plugin::boot();
if (is_admin()) {
    JGM_Admin::boot();
}

register_deactivation_hook(__FILE__, ['JGM_Plugin', 'deactivate']);
