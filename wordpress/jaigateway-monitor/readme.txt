=== JaiGateway Monitor ===
Contributors: jaigateway
Tags: error monitoring, uptime, cron, performance, debugging
Requires at least: 5.8
Tested up to: 6.8
Requires PHP: 8.0
Stable tag: 1.0.0
License: MIT
License URI: https://opensource.org/licenses/MIT

Know when your WordPress site breaks: PHP errors with the plugin or theme that caused them, JavaScript errors, WP-Cron that stopped, and page speed.

== Description ==

JaiGateway Monitor sends what goes wrong on your site to your own JaiGateway Monitor account, where errors are grouped, explained and alerted on (email, Slack, PagerDuty, ntfy, webhooks).

* **PHP errors**: fatal errors and exceptions, tagged with the plugin or theme that caused them (for example `plugin:woocommerce`), so you know who to blame in seconds.
* **JavaScript errors**: what your visitors hit in their browsers, with readable stack traces.
* **WP-Cron watchdog**: an hourly ping proves WP-Cron is alive. If scheduled posts, backups or shop jobs silently stop, you are alerted.
* **Email failures**: when `wp_mail()` cannot send (recipient addresses are removed).
* **Page speed**: Core Web Vitals (LCP, INP, CLS, FCP, TTFB) from real visitors, per page type.
* **Server performance**: how long front pages, admin pages, AJAX and REST calls take, the slowest database queries (values removed) and slow calls to outside services such as payment gateways and APIs.

Private by design: passwords, cookies, authorization headers and card numbers are removed before anything leaves your site; user names and emails are never sent (optionally only the numeric user id). Nothing is sent until you add your server address and key. The browser scripts are served from your own site; nothing is loaded from other servers.

This plugin needs a JaiGateway Monitor account (https://monitor.jaigateway.com).

== Installation ==

1. Upload the plugin and activate it.
2. In your JaiGateway Monitor dashboard, open **API Keys** and create a key with the **Write** permission.
3. Open **Settings → JaiGateway Monitor**, enter your server address and the key, save, and press **Send a test error**.
4. Optional: create a Cron job (every 1 hour, 15 minutes grace) in the dashboard and paste its ping address under WP-Cron.

To keep the key out of the database, add this to wp-config.php instead:

`define('JGM_HOST', 'https://monitor.jaigateway.com');`
`define('JGM_API_KEY', 'ak_…');`

== Frequently Asked Questions ==

= Does it slow my site down? =
Errors are sent in the same request only when one happens (2 second limit). Page-speed data is batched in the visitor's browser. Server performance monitoring is off by default (it keeps a list of each request's database queries in memory, which adds a little work).

= Which key should I use? =
A key with Write permission, one per site. It can only send data; it cannot read anything.

= Does it work on multisite? =
Yes, activate it per site or network-wide with a key per site (use wp-config constants for network-wide).

== Changelog ==

= 1.0.0 =
* First release: PHP and JavaScript errors, culprit plugin/theme tagging, WP-Cron watchdog, email failures, page speed, server response times, settings page with connection test.
