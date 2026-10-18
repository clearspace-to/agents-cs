"""Read-only API usage reports. Never sum sessions on top of the per-model ledger.

Canonical Hermes input/cache-write/cache-read/output buckets are disjoint;
reasoning is a subset of output. Cross-period dollars are provisionally allocated.
No message content, credentials, billing URLs or session IDs leave this module.
"""
from datetime import datetime, timedelta
from contextlib import closing
import math
from bisect import bisect_left, bisect_right
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('America/Toronto')
PERIODS = {'today', 'week', 'month', 'last7'}
TOKEN_FIELDS = ('input', 'output', 'cache_read', 'cache_write', 'reasoning', 'calls')
COLUMNS = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens', 'api_call_count')
TASKS = {'compression': 'Compression', 'approval': 'Approval checks', 'background_review': 'Background reviews',
         'title_generation': 'Chat titles', 'vision': 'Vision'}


def window(period, now):
    if period not in PERIODS:
        raise ValueError('Unknown usage period')
    local = datetime.fromtimestamp(now, ZONE)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'week':
        start -= timedelta(days=(start.weekday() + 1) % 7)
    elif period == 'month':
        start = start.replace(day=1)
    elif period == 'last7':
        start -= timedelta(days=6)
    return start.timestamp(), now


def metrics():
    return dict(cost=0.0, **{k: 0 for k in TOKEN_FIELDS}, rows=0, unknown_costs=0, included_calls=0)


def known_cost(row):
    value = row['estimated_cost_usd']
    status = row['cost_status']
    return (value is not None and math.isfinite(value) and value >= 0 and status != 'unknown'
            and (value > 0 or status in {'actual', 'estimated', 'included'}))


def add(target, row, lower=0.0, upper=1.0):
    target['rows'] += 1
    for key, column in zip(TOKEN_FIELDS, COLUMNS):
        target[key] += round(int(row[column] or 0) * upper) - round(int(row[column] or 0) * lower)
    cost = row['estimated_cost_usd']
    known = known_cost(row)
    if known:
        target['cost'] += cost * (upper - lower)
    else:
        target['unknown_costs'] += 1
    if row['cost_status'] == 'included':
        target['included_calls'] += round(row['api_call_count'] * upper) - round(row['api_call_count'] * lower)


def finish(value):
    if value['rows'] and value['unknown_costs'] == value['rows']:
        value['cost'] = None
    return value


def work_key(row):
    task = row['task'] or ''
    if task:
        return 'task:' + task, TASKS.get(task, task.replace('_', ' ').capitalize()), 'Background task'
    source = row['session_source']
    if source == 'cron':
        return 'scheduled', 'Scheduled jobs', 'Model usage in scheduled runs'
    if source in ('desktop', 'cli', 'tui', 'telegram', 'discord', 'slack', 'whatsapp', 'sms', 'api_server', 'signal'):
        return 'conversations', 'Conversations', 'Includes model calls that use tools'
    return 'other', 'Other runs', 'Delegated, one-shot or unclassified work'


def allocation(row, start, end, activity, now):
    """Return cumulative shares at boundaries so adjacent periods reconcile.

    Assistant timestamps are activity weights, not per-call billing evidence.
    Never freeze a closed session's estimate: every report recomputes from source.
    """
    first, last = row['first_seen'], row['last_seen']
    reliable = (first is not None and last is not None and
                math.isfinite(first) and math.isfinite(last) and last >= first)
    if not reliable:
        first, last = row['session_start'], row['session_last']
        first = first if first is not None and math.isfinite(first) else None
        last = last if last is not None and math.isfinite(last) else None
        first, last = first if first is not None else last, last if last is not None else first
        if first is None or last is None or last < first:
            # No usable date: assign to the current report's as-of day, visibly provisional.
            first = last = now
    if last < start or first > end:
        return 0.0, 0.0, None
    if first >= start and last <= end:
        return 0.0, 1.0, None if reliable else 'session date fallback'
    stamps = activity(row['session_id'])
    stamps = stamps[bisect_left(stamps, first):bisect_right(stamps, last)]
    if stamps:
        return (bisect_left(stamps, start) / len(stamps),
                bisect_right(stamps, end) / len(stamps), 'assistant activity')
    if last == first:
        return 0.0, 1.0, 'session date fallback'
    return (max(0.0, min(1.0, (start-first)/(last-first))),
            max(0.0, min(1.0, (end-first)/(last-first))), 'elapsed time')


def read_bot(profile, name, path, start, end):
    bot = dict(id=profile, name=name, available=True, totals=metrics(), models=[], work=[],
               unallocated_cost=0.0, unallocated_rows=0, allocated_cost=0.0, allocated_rows=0, warnings=[])
    models, work = {}, {}
    def issue(message):
        if message not in bot['warnings']:
            bot['warnings'].append(message)
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            required = set(COLUMNS) | {'session_id', 'model', 'billing_provider', 'task', 'estimated_cost_usd', 'cost_status', 'first_seen', 'last_seen'}
            if not required.issubset({r[1] for r in db.execute('PRAGMA table_info(session_model_usage)')}):
                raise ValueError('Unsupported usage schema')
            # Bound read time too: a large or damaged profile must not stall all reports.
            import time
            deadline = time.monotonic() + 3
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            message_columns = {r[1] for r in db.execute('PRAGMA table_info(messages)')}
            activity_cache = {}
            def activity(sid):
                if sid not in activity_cache:
                    activity_cache[sid] = ([r[0] for r in db.execute(
                        "SELECT timestamp FROM messages WHERE session_id=? AND role='assistant' AND timestamp IS NOT NULL ORDER BY timestamp", (sid,))
                        if math.isfinite(r[0])] if {'session_id', 'role', 'timestamp'}.issubset(message_columns) else [])
                return activity_cache[sid]
            ledger_columns = {r[1] for r in db.execute('PRAGMA table_info(session_model_usage)')}
            actual = 'u.actual_cost_usd' if 'actual_cost_usd' in ledger_columns else 'NULL'
            rows = db.execute(f'''SELECT u.session_id,u.model,u.billing_provider,u.task,u.input_tokens,u.output_tokens,
                u.cache_read_tokens,u.cache_write_tokens,u.reasoning_tokens,u.api_call_count,
                u.estimated_cost_usd,u.cost_status,u.first_seen,u.last_seen,
                s.source AS session_source,s.started_at AS session_start,s.last_activity_at AS session_last,
                {actual} AS actual_cost FROM session_model_usage u
                LEFT JOIN sessions s ON s.id=u.session_id''')
            for row in rows:
                row = dict(row)
                actual_cost = row['actual_cost']
                if (actual_cost is not None and math.isfinite(actual_cost) and actual_cost >= 0
                        and (actual_cost > 0 or row['cost_status'] == 'actual')):
                    row['estimated_cost_usd'], row['cost_status'] = actual_cost, 'actual'
                for col in COLUMNS:
                    if row[col] is None or row[col] < 0:
                        raise ValueError('Invalid usage counters')
                lower, upper, method = allocation(row, start, end, activity, end)
                if upper <= lower:
                    continue
                if method:
                    bot['allocated_rows'] += 1
                    if known_cost(row):
                        bot['allocated_cost'] += row['estimated_cost_usd'] * (upper-lower)
                    issue('Some spending is allocated using ' + method + '. It is included in the total and recalculated as records change.')
                model, provider = row['model'] or 'Unknown model', row['billing_provider'] or 'Unknown provider'
                key = (model, provider)
                if key not in models:
                    models[key] = dict(id=str(len(models)), name=model, provider=provider, totals=metrics(), work=[])
                wid, label, detail = work_key(row)
                if wid not in work:
                    work[wid] = dict(id=wid, name=label, detail=detail, totals=metrics())
                group = next((w for w in models[key]['work'] if w['id'] == wid), None)
                if group is None:
                    group = dict(id=wid, name=label, detail=detail, totals=metrics())
                    models[key]['work'].append(group)
                for target in (bot['totals'], models[key]['totals'], work[wid]['totals'], group['totals']):
                    add(target, row, lower, upper)
            missing = db.execute('''SELECT count(*) FROM sessions s
                WHERE (s.started_at>=? OR s.last_activity_at>=?) AND
                (COALESCE(s.api_call_count,0)>0 OR COALESCE(s.estimated_cost_usd,0)>0)
                AND NOT EXISTS (SELECT 1 FROM session_model_usage u WHERE u.session_id=s.id AND u.task='')''',
                                 (start, start)).fetchone()[0]
            if missing:
                issue('Active sessions have no main-loop usage ledger. The recorded estimate is incomplete.')
            if bot['totals']['unknown_costs']:
                issue('Some model usage has no known price. Its tokens are included, but its cost is unavailable.')
            # A readable empty ledger with evidence of missing usage is not a known zero.
            if missing and not bot['totals']['rows']:
                bot['totals']['cost'] = None
    except (sqlite3.Error, OSError, ValueError, KeyError, IndexError, TypeError):
        bot.update(available=False, totals=metrics(), unallocated_cost=0.0, unallocated_rows=0, allocated_cost=0.0, allocated_rows=0,
                   warnings=['Usage could not be read. The database may be unavailable or use an unsupported schema.'])
        bot['totals']['cost'] = None
        return bot
    for model in models.values():
        finish(model['totals'])
        for group in model['work']:
            finish(group['totals'])
        model['work'].sort(key=lambda w: -(w['totals']['cost'] or 0))
    for group in work.values():
        finish(group['totals'])
    bot['models'] = sorted(models.values(), key=lambda m: -(m['totals']['cost'] or 0))
    bot['work'] = sorted(work.values(), key=lambda w: -(w['totals']['cost'] or 0))
    finish(bot['totals'])
    return bot


def report(home, period, now, name_for):
    start, end = window(period, now)
    homes = [('default', home)] + [(p.name, p) for p in sorted((home / 'profiles').glob('*')) if p.is_dir()]
    bots, seen = [], set()
    for profile, folder in homes:
        path = folder / 'state.db'
        if path.resolve() in seen:
            bot = dict(id=profile, name=name_for(profile), available=False, totals=metrics(), models=[], work=[],
                       unallocated_cost=0.0, unallocated_rows=0, allocated_cost=0.0, allocated_rows=0, warnings=['Database duplicates another bot; excluded.'])
            bot['totals']['cost'] = None
        else:
            seen.add(path.resolve())
            bot = read_bot(profile, name_for(profile), path, start, end)
        bots.append(bot)
    bots.sort(key=lambda b: (b['totals']['cost'] is None, -(b['totals']['cost'] or 0), b['name']))
    known = [b['totals']['cost'] for b in bots if b['totals']['cost'] is not None]
    return dict(period=period, start=start, end=end, timezone=str(ZONE), currency='USD',
                cost=sum(known) if known else None, partial=any(b['warnings'] for b in bots),
                unallocated_cost=sum(b['unallocated_cost'] for b in bots),
                unallocated_rows=sum(b['unallocated_rows'] for b in bots),
                allocated_cost=sum(b['allocated_cost'] for b in bots),
                allocated_rows=sum(b['allocated_rows'] for b in bots), bots=bots)
