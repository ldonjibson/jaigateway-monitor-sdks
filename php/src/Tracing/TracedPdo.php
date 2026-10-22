<?php

namespace Insight\Tracing;

use Insight\Insight;

/**
 * Opt-in PDO subclass for apps without Laravel: every query(), exec() and prepared execute() becomes
 * a "db.query" span. Only the SQL string is used (normalised before it leaves the process); bound
 * values and fetched rows are never looked at.
 *
 *   $pdo = new \Insight\Tracing\TracedPdo($dsn, $user, $pass, $options);
 */
class TracedPdo extends \PDO
{
    private string $system = '';

    public function __construct(string $dsn, ?string $username = null, ?string $password = null, ?array $options = null)
    {
        parent::__construct($dsn, $username, $password, $options);
        try {
            $this->system = (string) $this->getAttribute(\PDO::ATTR_DRIVER_NAME);
            $this->setAttribute(\PDO::ATTR_STATEMENT_CLASS, [TracedPdoStatement::class, [$this->system]]);
        } catch (\Throwable $e) {
            // tracing of prepared statements is then simply off
        }
    }

    public function query(string $query, ?int $fetchMode = null, mixed ...$fetchModeArgs): \PDOStatement|false
    {
        $start = microtime(true);
        try {
            $result = $fetchMode === null ? parent::query($query) : parent::query($query, $fetchMode, ...$fetchModeArgs);
        } catch (\Throwable $e) {
            self::record($this->system, $query, $start, false);
            throw $e;
        }
        self::record($this->system, $query, $start, $result !== false);

        return $result;
    }

    public function exec(string $statement): int|false
    {
        $start = microtime(true);
        try {
            $result = parent::exec($statement);
        } catch (\Throwable $e) {
            self::record($this->system, $statement, $start, false);
            throw $e;
        }
        self::record($this->system, $statement, $start, $result !== false);

        return $result;
    }

    /** @internal */
    public static function record(string $system, string $sql, float $start, bool $ok): void
    {
        try {
            $now = microtime(true);
            Insight::recordSpan('db.query', $sql, ($now - $start) * 1000, $ok, $system !== '' ? ['db.system' => $system] : [], $start);
        } catch (\Throwable $e) {
            // never throw into the app
        }
    }
}
