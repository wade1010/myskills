import random
import uuid
import datetime

# ───────────── 配置 ─────────────
DETAIL_MIN = 360    # request_logs 覆盖最近 6 小时（支撑 live / token 趋势 ≤6h）
AGG_MIN   = 2880    # stats_minute / stats_token_hour 覆盖最近 2 天（长时段趋势 / 区间排行）
CACHE_RATE = 0.35   # 缓存命中比例（让 cache_hits 列有数）
APIS = ['index_daily', 'daily', 'stk_mins', 'etf_mins']

# ───────────── 读取 token ─────────────
toks = []
with open('D:/workspace/ts_proxy/.workbuddy/tokmap.txt') as f:
    for line in f:
        p = line.rstrip('\n').split('\t')
        if len(p) >= 3 and p[0].strip().isdigit():
            toks.append((int(p[0]), int(p[1]), p[2].strip()))
print('tokens loaded:', len(toks))

now = datetime.datetime.now()

# ───────────── 单条调用的字段生成 ─────────────
def gen_call(api):
    """返回 (from_cache, status, ob, dur, reject_sql)"""
    if random.random() < CACHE_RATE:
        # 命中缓存：不走上游，延迟极低
        return 1, 200, random.randint(200, 50000), random.randint(1, 40), "''"
    # 未命中：真实上游
    err_rate = 0.25 if api == 'stk_mins' else (0.08 if api == 'etf_mins' else 0.04)
    is_err = random.random() < err_rate
    status = 400 if is_err else 200
    ob = random.randint(200, 50000) if status == 200 else 0
    dur = random.randint(80, 8000)
    reject = "'rate_limited'" if is_err else "''"
    return 0, status, ob, dur, reject

# ───────────── 聚合累加容器 ─────────────
req_rows = []   # request_logs 的 VALUES 片段
sm = {}         # stats_minute:  (user_id, api, km)       -> bucket
sth = {}        # stats_token_hour: (token_id, prefix, user_id, kh) -> bucket
std = {}        # stats_token_day:  (token_id, prefix, user_id, kd) -> bucket

def new_bucket():
    return {'calls': 0, 'errors': 0, 'traffic': 0, 'cache': 0, 'up': 0,
            'dur': 0, 'up_dur': 0, 'up_n': 0, 'apis': set()}

def add_bucket(b, c_calls, c_err, c_traffic, c_cache, c_up, dur_sum, up_dur, up_n, api):
    b['calls'] += c_calls
    b['errors'] += c_err
    b['traffic'] += c_traffic
    b['cache'] += c_cache
    b['up'] += c_up
    b['dur'] += dur_sum
    b['up_dur'] += up_dur
    b['up_n'] += up_n
    b['apis'].add(api)

# ───────────── 主循环 ─────────────
for km in range(AGG_MIN):                       # km = 多少分钟之前
    bucket_time = now - datetime.timedelta(minutes=km)
    hour = bucket_time.hour
    # 简单的日内负载曲线：上午/下午忙，凌晨淡
    load = 2.2 if (9 <= hour <= 11 or 14 <= hour <= 15) else (0.4 if 0 <= hour <= 6 else 1.0)
    kh = km // 60
    kd = km // 1440
    for (tid, uid, pref) in toks:
        for api in APIS:
            calls = max(1, int(random.randint(1, 8) * load))
            if random.random() < 0.10:          # 偶发尖峰
                calls += random.randint(5, 25)
            c_calls = c_err = c_traffic = c_cache = c_up = 0
            dur_sum = up_dur = up_n = 0
            for _ in range(calls):
                fc, st, ob, dur, rj = gen_call(api)
                c_calls += 1
                if st >= 400:
                    c_err += 1
                c_traffic += ob
                if fc == 1:
                    c_cache += 1
                else:
                    if rj == "''":               # 上游调用（非拒绝）
                        c_up += 1
                        up_dur += dur
                        up_n += 1
                dur_sum += dur
                if km < DETAIL_MIN:             # 仅最近 6h 写明细
                    sec = random.randint(0, 59)
                    rid = 'SIM' + uuid.uuid4().hex[:14]
                    params = "'{\"token\":\"%s\",\"api_name\":\"%s\"}'" % (pref, api)
                    req_rows.append("('%s',%d,'%s',%d,'192.168.1.200','%s',%s,%d,%d,%d,%d,'',%s,NOW() - INTERVAL %d MINUTE + INTERVAL %d SECOND)"
                                    % (rid, uid, pref, tid, api, params, st, ob, dur, fc, rj, km, sec))
            # stats_minute（按 user 聚合）
            mb = sm.setdefault((uid, api, km), new_bucket())
            add_bucket(mb, c_calls, c_err, c_traffic, c_cache, c_up, dur_sum, up_dur, up_n, api)
            # stats_token_hour
            hb = sth.setdefault((tid, pref, uid, kh), new_bucket())
            add_bucket(hb, c_calls, c_err, c_traffic, c_cache, c_up, dur_sum, up_dur, up_n, api)
            # stats_token_day
            dbk = std.setdefault((tid, pref, uid, kd), new_bucket())
            add_bucket(dbk, c_calls, c_err, c_traffic, c_cache, c_up, dur_sum, up_dur, up_n, api)

# ───────────── 写出 SQL ─────────────
out = []
out.append("SET autocommit=0;")
out.append("SET unique_checks=0;")
out.append("SET foreign_key_checks=0;")

# request_logs：批量多 VALUES
def flush_req(batch):
    out.append(
        "INSERT INTO request_logs "
        "(request_id,user_id,token_prefix,token_id,source_ip,api_name,request_params,"
        "response_status,response_bytes,duration_ms,from_cache,cache_source,reject_reason,created_at) "
        "VALUES " + ",".join(batch) + " ON DUPLICATE KEY UPDATE request_id=request_id;"
    )

i = 0
B = 1000
for row in req_rows:
    if i == 0:
        batch = []
    batch.append(row)
    i += 1
    if i == B:
        flush_req(batch)
        i = 0
if i > 0:
    flush_req(batch)

# 聚合表按块 flush，避免单条巨 INSERT 撞 max_allowed_packet
CHUNK = 2000

def _flush_stats(table, cols, rows, dup_update):
    """rows: list of VALUES 字符串；按 CHUNK 拆成多条 INSERT 语句。"""
    for i in range(0, len(rows), CHUNK):
        chunk = rows[i:i + CHUNK]
        out.append("INSERT INTO %s %s VALUES %s ON DUPLICATE KEY UPDATE %s;"
                   % (table, cols, ",".join(chunk), dup_update))

def emit_minute():
    cols = ("(user_id,api_name,period_start,call_count,error_count,avg_duration_ms,traffic_bytes,"
            "cache_hits,upstream_calls,upstream_avg_ms)")
    dup = ("call_count=VALUES(call_count),error_count=VALUES(error_count),"
           "avg_duration_ms=VALUES(avg_duration_ms),traffic_bytes=VALUES(traffic_bytes),"
           "cache_hits=VALUES(cache_hits),upstream_calls=VALUES(upstream_calls),"
           "upstream_avg_ms=VALUES(upstream_avg_ms)")
    rows = []
    for (uid, api, km), b in sm.items():
        if b['calls'] == 0:
            continue
        avg = b['dur'] // b['calls']
        up_avg = b['up_dur'] // b['up_n'] if b['up_n'] else 0
        rows.append("(%d,'%s',DATE_FORMAT(NOW() - INTERVAL %d MINUTE,'%%Y-%%m-%%d %%H:%%i:00'),%d,%d,%d,%d,%d,%d,%d)" % (
            uid, api, km, b['calls'], b['errors'], avg, b['traffic'], b['cache'], b['up'], up_avg))
    _flush_stats("stats_minute", cols, rows, dup)

def emit_token_hour():
    cols = ("(token_id,token_prefix,user_id,period_start,call_count,error_count,avg_duration_ms,"
            "traffic_bytes,cache_hits,upstream_calls,upstream_avg_ms,api_count,ip_count)")
    dup = ("call_count=VALUES(call_count),error_count=VALUES(error_count),"
           "avg_duration_ms=VALUES(avg_duration_ms),traffic_bytes=VALUES(traffic_bytes),"
           "cache_hits=VALUES(cache_hits),upstream_calls=VALUES(upstream_calls),"
           "upstream_avg_ms=VALUES(upstream_avg_ms),api_count=VALUES(api_count),ip_count=VALUES(ip_count)")
    rows = []
    for (tid, pref, uid, kh), b in sth.items():
        if b['calls'] == 0:
            continue
        avg = b['dur'] // b['calls']
        up_avg = b['up_dur'] // b['up_n'] if b['up_n'] else 0
        rows.append("(%d,'%s',%d,DATE_FORMAT(NOW() - INTERVAL %d HOUR,'%%Y-%%m-%%d %%H:00:00'),%d,%d,%d,%d,%d,%d,%d,%d,1)" % (
            tid, pref, uid, kh, b['calls'], b['errors'], avg, b['traffic'], b['cache'], b['up'], up_avg, len(b['apis'])))
    _flush_stats("stats_token_hour", cols, rows, dup)

def emit_token_day():
    cols = ("(token_id,token_prefix,user_id,period_start,call_count,error_count,avg_duration_ms,"
            "traffic_bytes,cache_hits,upstream_calls,upstream_avg_ms,api_count,ip_count)")
    dup = ("call_count=VALUES(call_count),error_count=VALUES(error_count),"
           "avg_duration_ms=VALUES(avg_duration_ms),traffic_bytes=VALUES(traffic_bytes),"
           "cache_hits=VALUES(cache_hits),upstream_calls=VALUES(upstream_calls),"
           "upstream_avg_ms=VALUES(upstream_avg_ms),api_count=VALUES(api_count),ip_count=VALUES(ip_count)")
    rows = []
    for (tid, pref, uid, kd), b in std.items():
        if b['calls'] == 0:
            continue
        avg = b['dur'] // b['calls']
        up_avg = b['up_dur'] // b['up_n'] if b['up_n'] else 0
        rows.append("(%d,'%s',%d,DATE(NOW() - INTERVAL %d DAY),%d,%d,%d,%d,%d,%d,%d,%d,1)" % (
            tid, pref, uid, kd, b['calls'], b['errors'], avg, b['traffic'], b['cache'], b['up'], up_avg, len(b['apis'])))
    _flush_stats("stats_token_day", cols, rows, dup)

emit_minute()
emit_token_hour()
emit_token_day()

out.append("COMMIT;")

with open('D:/workspace/ts_proxy/.workbuddy/sim_insert.sql', 'w') as f:
    f.write("\n".join(out) + "\n")

print("req=%d  stats_minute=%d  token_hour=%d  token_day=%d" % (len(req_rows), len(sm), len(sth), len(std)))
