<?php
// Runs when the plugin is deleted in wp-admin: remove everything it stored.
if (!defined('WP_UNINSTALL_PLUGIN')) {
    exit;
}
delete_option('jgm_settings');
delete_transient('jgm_test_result');
$next = wp_next_scheduled('jgm_cron_ping');
if ($next) {
    wp_unschedule_event($next, 'jgm_cron_ping');
}
