<?php
/** Plugin Name: Bad Plugin (test fixture) */
add_action('init', function () {
    $mode = isset($_GET['jgm_boom']) ? $_GET['jgm_boom'] : '';
    if ($mode === 'fatal') {
        bad_plugin_undefined_function(); // fatal: call to undefined function
    }
    if ($mode === 'http') {
        wp_remote_get('http://127.0.0.1/?jgm_inner=1'); // an outside call (to the site itself)
    }
    if ($mode === 'exception') {
        throw new RuntimeException('bad plugin exploded');
    }
});
