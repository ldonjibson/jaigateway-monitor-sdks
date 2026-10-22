<?php

namespace Insight\Tracing;

/** Statement class installed by TracedPdo; times execute(). Uses only $queryString. */
class TracedPdoStatement extends \PDOStatement
{
    private string $system;

    protected function __construct(string $system = '')
    {
        $this->system = $system;
    }

    public function execute(?array $params = null): bool
    {
        $start = microtime(true);
        try {
            $result = parent::execute($params);
        } catch (\Throwable $e) {
            TracedPdo::record($this->system, (string) $this->queryString, $start, false);
            throw $e;
        }
        TracedPdo::record($this->system, (string) $this->queryString, $start, $result);

        return $result;
    }
}
