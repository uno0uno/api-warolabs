# Operational observability runbook

Issue: api-warolabs#567

Use this runbook when a customer reports slowness, failed buttons, missing
updates, or "the system was down". The goal is to separate expected auth/SSE
noise from real backend, database, host, or proxy failures.

## 1. Define the window

Convert the report to UTC before querying logs. Keep both values in the incident
note.

```text
Customer report: 2026-06-27 09:00-11:00 America/Bogota
UTC window:      2026-06-27 14:00-16:00 UTC
Tenant/user:     <tenant or user if known>
Symptoms:        <button, route, module, exact message>
```

## 2. API request logs

New API request logs use a parseable shape:

```text
request method=GET path=/orders status_code=200 duration_ms=12.34 severity=ok tenant=Demo user=<id>
```

Severity buckets:

| Severity | Meaning |
|---|---|
| `ok` | 2xx/3xx request |
| `auth_expected` | 401 caused by missing/expired/invalid session |
| `client_error` | non-auth 4xx |
| `server_error` | 5xx or unexpected middleware failure |
| `sse_stream` | successful notification stream request |

Expected auth volume is not the same as downtime. Investigate 5xx,
`server_error`, schema errors, and sustained high latency first.

## 3. `waro_logs` SQL checks

Daily volume:

```sql
SELECT
  date_trunc('day', created_at) AS day_utc,
  count(*) AS total_logs,
  count(*) FILTER (WHERE level ILIKE 'error') AS raw_errors,
  count(*) FILTER (
    WHERE level ILIKE 'error'
      AND message NOT ILIKE '%No session found%'
      AND message NOT ILIKE '%Valid session required%'
      AND message NOT ILIKE '%Session validation failed%'
  ) AS errors_excluding_auth
FROM waro_logs
WHERE created_at >= TIMESTAMPTZ '2026-06-27 00:00:00+00'
  AND created_at <  TIMESTAMPTZ '2026-06-28 00:00:00+00'
GROUP BY 1
ORDER BY 1;
```

Hourly spike view:

```sql
SELECT
  date_trunc('hour', created_at) AS hour_utc,
  count(*) AS total,
  count(*) FILTER (WHERE message ILIKE '%severity=server_error%') AS server_errors,
  count(*) FILTER (WHERE message ILIKE '%severity=auth_expected%') AS expected_auth,
  count(*) FILTER (WHERE message ILIKE '%notifications/stream%') AS sse_stream,
  count(*) FILTER (
    WHERE message ILIKE '%column % does not exist%'
       OR message ILIKE '%relation % does not exist%'
       OR message ILIKE '%duplicate key value violates unique constraint%'
  ) AS db_signatures
FROM waro_logs
WHERE created_at >= TIMESTAMPTZ '2026-06-27 14:00:00+00'
  AND created_at <  TIMESTAMPTZ '2026-06-27 16:00:00+00'
GROUP BY 1
ORDER BY 1;
```

Slow endpoints from parseable request logs:

```sql
SELECT
  regexp_replace(message, '.* path=([^ ]+) .*', '\1') AS path,
  count(*) AS hits,
  max((regexp_replace(message, '.* duration_ms=([0-9.]+) .*', '\1'))::numeric) AS max_ms,
  percentile_cont(0.95) WITHIN GROUP (
    ORDER BY (regexp_replace(message, '.* duration_ms=([0-9.]+) .*', '\1'))::numeric
  ) AS p95_ms
FROM waro_logs
WHERE created_at >= TIMESTAMPTZ '2026-06-27 14:00:00+00'
  AND created_at <  TIMESTAMPTZ '2026-06-27 16:00:00+00'
  AND message LIKE 'request %duration_ms=%'
GROUP BY 1
ORDER BY p95_ms DESC
LIMIT 20;
```

## 4. Docker API logs

Do not assume a fixed container name. Discover the compose service/container,
then read recent logs.

```bash
cd /home/saifer/api_warocol.com
docker compose ps
docker compose logs --since "2026-06-27T14:00:00Z" --until "2026-06-27T16:00:00Z" web
```

If compose metadata is unavailable:

```bash
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'api|warocol'
docker logs --since "2026-06-27T14:00:00Z" --until "2026-06-27T16:00:00Z" <container>
```

## 5. Postgres health

Cache hit and deadlocks:

```sql
SELECT
  round(100 * sum(blks_hit)::numeric / nullif(sum(blks_hit + blks_read), 0), 2) AS hit_pct,
  sum(deadlocks) AS deadlocks
FROM pg_stat_database
WHERE datname = current_database();
```

Waiting locks:

```sql
SELECT pid, wait_event_type, wait_event, state, now() - query_start AS age, query
FROM pg_stat_activity
WHERE wait_event_type IS NOT NULL
ORDER BY age DESC
LIMIT 20;
```

Long active queries:

```sql
SELECT pid, state, now() - query_start AS age, left(query, 300) AS query
FROM pg_stat_activity
WHERE state <> 'idle'
ORDER BY age DESC
LIMIT 20;
```

## 6. Host metrics with sysstat

Check CPU/load and disk around the UTC window.

```bash
sar -u -f /var/log/sysstat/sa27
sar -q -f /var/log/sysstat/sa27
sar -d -f /var/log/sysstat/sa27
```

Healthy examples from #562 were high CPU idle, low load, no waiting Postgres
locks, no deadlocks, and cache hit around 99%.

## 7. Nginx access/error logs

Support checks must not depend on an interactive sudo prompt. First test
non-interactive access:

```bash
sudo -n tail -n 20 /var/log/nginx/access.log
sudo -n tail -n 20 /var/log/nginx/error.log
```

If either command fails with a password/permission error, record it in the
incident and request one of these operational fixes:

```text
- add the deploy/support user to a read-only log group for /var/log/nginx/*.log
- add a sudoers rule for passwordless read-only tail/grep on nginx logs
- ship Nginx access/error logs into the same central log store as waro_logs
```

Useful Nginx checks once readable:

```bash
sudo -n grep ' 5[0-9][0-9] ' /var/log/nginx/access.log | tail -n 50
sudo -n grep ' 499 ' /var/log/nginx/access.log | tail -n 50
sudo -n tail -n 100 /var/log/nginx/error.log
```

## 8. Decision guide

| Evidence | Interpretation | Next action |
|---|---|---|
| High `auth_expected`, low 5xx | Sessions expired/missing; not platform downtime | Check session UX/auth batch behavior |
| `sse_stream` or socket disconnects only | Normal browser reconnect/close unless paired with 5xx | Check notification lifecycle only if duplicated |
| 5xx or `server_error` spike | Backend failure | Inspect endpoint logs and stack traces |
| `column does not exist` / `relation does not exist` | Schema mismatch | Verify migrations/dbdoc |
| High latency, host healthy, DB healthy | Application endpoint bottleneck | Rank slow endpoints by p95 |
| Host CPU/load/disk saturated | Infrastructure bottleneck | Escalate capacity/host investigation |

## 9. Incident note template

```text
Window:
Tenant/user:
Symptoms:
API request buckets:
Slow endpoints:
DB health:
Docker evidence:
Nginx evidence:
Conclusion:
Follow-up issue/PR:
```
