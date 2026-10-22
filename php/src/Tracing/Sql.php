<?php

namespace Insight\Tracing;

/**
 * Span text hygiene, a port of normalizeSql / normalizeHttp in the server (server/perf.ts).
 * SQL leaves this SDK with every literal removed; bind values are never looked at.
 */
final class Sql
{
    public const MAX = 300;

    public static function normalize(string $sql): string
    {
        $s = $sql;
        $s = self::re('#/\*.*?\*/#s', ' ', $s);
        $s = self::re('/--[^\n]*/', ' ', $s);
        $s = self::re("/'(?:[^']|'')*'/", '?', $s);
        $s = self::re("/\\bE'(?:[^'\\\\]|\\\\.)*'/", '?', $s);
        $s = self::re('/\b0x[0-9a-f]+\b/i', '?', $s);
        $s = self::re('/(?<![\w$.])-?\d+(?:\.\d+)?(?:e[+-]?\d+)?\b/i', '?', $s);
        $s = self::re('/\bIN\s*\(\s*\?(?:\s*,\s*\?)*\s*\)/i', 'IN (?)', $s);
        $s = self::re('/\bVALUES\s*\(\s*\?(?:\s*,\s*\?)*\s*\)(?:\s*,\s*\(\s*\?(?:\s*,\s*\?)*\s*\))*/i', 'VALUES (?)', $s);
        $s = trim(self::re('/\s+/', ' ', $s));
        if (strlen($s) > self::MAX) {
            $s = substr($s, 0, 297) . '...';
        }

        return $s;
    }

    /** "GET https://user:pw@api.stripe.com/v1/charges?x=1" -> "GET api.stripe.com". */
    public static function http(string $method, string $url): string
    {
        return self::describeHttp(trim(strtoupper($method) . ' ' . $url));
    }

    public static function describeHttp(string $desc): string
    {
        $desc = trim($desc);
        $method = '';
        $target = $desc;
        if (preg_match('/^([A-Z]+)\s+(\S+)/', $desc, $m) === 1) {
            $method = $m[1];
            $target = $m[2];
        }

        return substr(($method !== '' ? $method . ' ' : '') . self::host($target), 0, self::MAX);
    }

    /** Host (with port when given) of a URL or "host/path"; never credentials, path or query. */
    public static function host(string $target): string
    {
        $t = preg_match('#^[a-z][a-z0-9+.-]*://#i', $target) === 1 ? $target : 'http://' . $target;
        $parts = @parse_url($t);
        if (is_array($parts) && isset($parts['host']) && $parts['host'] !== '') {
            return strtolower($parts['host']) . (isset($parts['port']) ? ':' . $parts['port'] : '');
        }
        $h = preg_split('#[/?\#]#', $target, 2);
        $h = $h !== false ? $h[0] : '';
        $at = strrpos($h, '@');

        return $at !== false ? substr($h, $at + 1) : $h;
    }

    private static function re(string $pattern, string $to, string $subject): string
    {
        $r = @preg_replace($pattern, $to, $subject);

        return is_string($r) ? $r : '?'; // on a regex failure (backtrack limit) send nothing readable
    }
}
