import json, urllib.request, datetime

BASE = "http://192.168.1.111:18001"
NOW = datetime.datetime.now()

def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=30))

def get(path, token, params=""):
    url = BASE + path + (("?" + params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    return json.load(urllib.request.urlopen(req, timeout=30))

def fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")

j = post("/admin/api/login", {"username": "admin", "password": "admin123"})
JWT = j["data"]["token"]
print("JWT ok, len=", len(JWT))

def summarize_token_ranking(params, label):
    d = get("/admin/api/stats/tokens/ranking", JWT, params)["data"]
    rows = d["rows"]
    ids = [r["token_id"] for r in rows]
    from collections import Counter
    dup = {k: v for k, v in Counter(ids).items() if v > 1}
    ch = sum(r["cache_hits"] for r in rows)
    total = sum(r['calls'] for r in rows)
    print(f"[{label}] gran={d['granularity']} rows={len(rows)} total={total} "
          f"dup={dup or 'NONE'} cache_hits_sum={ch} rows_with_cache={sum(1 for r in rows if r['cache_hits']>0)}")
    # token 8 / 1 样本
    for tid in (1, 8):
        for r in rows:
            if r["token_id"] == tid:
                print(f"    token {tid}: prefix={r['token_prefix']} calls={r['calls']} cache={r['cache_hits']} user={r['username']}")
                break

def summarize_api_ranking(params, label):
    d = get("/admin/api/stats/apis/ranking", JWT, params)["data"]
    rows = d["rows"]
    print(f"[{label}] gran={d['granularity']} rows={len(rows)} from={d['from']} to={d['to']}")
    for r in rows:
        cr = (r['cache_hits']*100//r['calls']) if r['calls'] else 0
        print(f"    {r['api_name']:12s} calls={r['calls']:7d} err={r['errors']:5d} cache={r['cache_hits']:7d}({cr}%) up={r['upstream_calls']:7d} users={r['users']}")

# 1) token 排行：今天（默认）
summarize_token_ranking("", "token-ranking TODAY")
# 2) token 排行：最近 2 天，hour
f2 = fmt(NOW - datetime.timedelta(days=2))
summarize_token_ranking(f"from={f2}&granularity=hour", "token-ranking 2d/hour")
# 3) token 排行：最近 3 天，day
f3 = fmt(NOW - datetime.timedelta(days=3))
summarize_token_ranking(f"from={f3}&granularity=day", "token-ranking 3d/day")

# 4) api live
d = get("/admin/api/stats/apis/live", JWT, "window=60")["data"]
print(f"[api-live 60min] total={d['total_calls']} rows={len(d['rows'])} window={d['window']}")
for r in d["rows"]:
    print(f"    {r['api_name']:12s} calls={r['calls']:6d} err={r['errors']:4d} cache={r['cache_hits']:6d} users={r['users']}")

# 5) api trend 最大窗口
d = get("/admin/api/stats/apis/trend", JWT, "window=360")["data"]
print(f"[api-trend 360min] minutes={len(d['minutes'])} series={len(d['series'])}")
for s in d["series"]:
    print(f"    {s['api_name']:12s} pts={len(s['calls'])} sum={sum(s['calls'])}")

# 6) api ranking 2 天 hour
summarize_api_ranking(f"from={f2}&granularity=hour", "api-ranking 2d/hour")

# 7) 新 token id=1 的 detail / trend
for tid, pref in [(1, "324a551f")]:
    d = get(f"/admin/api/stats/tokens/{tid}/detail", JWT, f"prefix={pref}&window=360")["data"]
    print(f"[token {tid} detail] calls={d['calls']} err={d['errors']} cache={d['cache_hits']} "
          f"top_apis={len(d['top_apis'])} recent={len(d['recent'])} token_name={d['token_name']!r} user={d['username']}")
    d2 = get(f"/admin/api/stats/tokens/{tid}/trend", JWT, f"prefix={pref}&window=360")["data"]
    print(f"[token {tid} trend] minutes={len(d2['minutes'])} sum_calls={sum(d2['calls'])}")
