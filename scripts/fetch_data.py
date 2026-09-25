#!/usr/bin/env python3
"""資金の温度計: データを集めて docs/data.json と docs/history.json を更新する。

使い方:
  python scripts/fetch_data.py          # 本番（APIから取得）
  python scripts/fetch_data.py --mock   # サンプルデータで docs/data.mock.json を作る
"""
import json
import math
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
MOCK = "--mock" in sys.argv
DATA_PATH = DOCS / ("data.mock.json" if MOCK else "data.json")
HIST_PATH = DOCS / "history.json"
HIST_MAX = 24 * 30  # 30日分（1時間ごと）
LONG_PATH = DOCS / ("longterm.mock.json" if MOCK else "longterm.json")
LONG_START = 1483228800  # 2017-01-01。長期チャートの価格はここから
LONG_REFRESH = 20 * 3600  # 価格と恐怖・強欲指数の全期間は1日1回だけ取り直す

CG_KEY = os.environ.get("COINGECKO_API_KEY", "")
CG_BASE = "https://api.coingecko.com/api/v3"
UA = {"User-Agent": "sector-heat/1.0"}

# (DefiLlamaでのチェーン名, 先物の銘柄) ― 好きに足し引きしてOK
CHAINS = [
    ("Ethereum", "ETH"), ("Solana", "SOL"), ("BSC", "BNB"), ("Tron", "TRX"),
    ("Base", None), ("Arbitrum", "ARB"), ("Sui", "SUI"), ("Avalanche", "AVAX"),
    ("Hyperliquid L1", "HYPE"), ("Aptos", "APT"), ("Polygon", "POL"),
    ("TON", "TON"), ("Bitcoin", "BTC"), ("XRPL", "XRP"), ("Stellar", "XLM"),
    ("Near", "NEAR"), ("Sei", "SEI"), ("OP Mainnet", "OP"),
]

# スコアの重み（合計1でなくてもOK）
WEIGHTS = {"stable_7d": 0.35, "dex_7d": 0.25, "tvl_7d": 0.2, "oi_24h": 0.2}
FR_HOT = 0.0005    # 0.05%/8h 以上は過熱扱い
FR_CALM = 0.0002   # 0.02%/8h 未満は落ち着いている扱い
OI_HOT = 25        # OIが24hで+25%以上は過熱扱い
MIN_CAT_MCAP = 3e8
MAX_CATS = 80
# 価格トレンドとOIのズレ
PX_MOVE = 1.0      # 価格が24hで±1%以上動いたら「動いた」扱い
OI_MOVE = 5        # 価格の影響を除いたOIが±5%以上で「増えた/減った」扱い
OI_SURGE = 10      # 価格が動かないのにOIが+10%以上なら「OIだけ急増」
FNG_GREED = 75     # 恐怖・強欲指数がこれ以上ならエントリー候補から外す
ENTRY_MIN_SCORE = 60  # 温度がこれ未満のチェーンは「条件そろい」にしない
ENTRY_MAX = 3         # 「条件そろい」は温度の高い順に最大この件数まで
# 注目トークン（各チェーンのDeFiトークン）
TOKENS_PER_CHAIN = 5
TOKEN_MIN_TVL = 5e6     # そのチェーン上のTVLがこれ未満のプロトコルは除外
TOKEN_MIN_MCAP = 1e7    # 時価総額がこれ未満のトークンは除外
TOKEN_SHARE = 0.5       # TVLの半分以上がそのチェーンにあるものだけ「そのチェーンのトークン」扱い
# トークンの値動きとTVLが結びつきにくい種類は除外（ブリッジは預かり資産、ステーキングは元の通貨がTVL）
TOKEN_SKIP_CATS = {"CEX", "Chain", "Bridge", "Canonical Bridge", "Cross Chain Bridge", "Bridge Aggregators",
                   "Liquid Staking", "Liquid Restaking", "Restaking", "Restaked BTC", "Indexes", "Basis Trading"}
TOKEN_WEIGHTS = {"tvl_7d": 0.4, "rel_7d": 0.35, "turnover": 0.25}
TOKEN_PUMP = 40         # 対BTCで7日+40%以上は「急騰後」


# ---------- 共通 ----------
def get(url, headers=None, params=None, tries=3):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers={**UA, **(headers or {})}, params=params, timeout=30)
            if r.status_code == 429:
                last = RuntimeError(f"429 rate limited: {url}")
                time.sleep(6 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last = e
            if getattr(e, "response", None) is not None and e.response.status_code in (403, 451):
                break  # 地域ブロックはリトライしても無駄
            time.sleep(2 * (i + 1))
    raise last


def safe(fn, default=None):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        print(f"  warn: {e}")
        return default


def pct(now, before):
    if now is None or not before:
        return None
    return (now / before - 1) * 100


def load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return default


def value_days_ago(points, days, now_ts):
    """points: [(ts, value), ...] 昇順。days日前以前で一番新しい値"""
    target = now_ts - days * 86400
    val = None
    for ts, v in points:
        if ts <= target and v is not None:
            val = v
        elif ts > target:
            break
    return val


def hist_value(hist, getter, days, now_ts):
    return value_days_ago([(s["ts"], safe(lambda s=s: getter(s))) for s in hist], days, now_ts)


# ---------- CoinGecko ----------
def cg(path, params=None):
    headers = {"x-cg-demo-api-key": CG_KEY} if CG_KEY else {}
    return get(CG_BASE + path, headers=headers, params=params)


def fetch_market():
    g = cg("/global")["data"]
    px = cg("/simple/price", {"ids": "bitcoin,ethereum", "vs_currencies": "usd",
                              "include_24hr_change": "true"})
    btc, eth = px["bitcoin"], px.get("ethereum") or {}
    cats = cg("/coins/categories")
    return {"btc_dom": g["market_cap_percentage"]["btc"], "btc_price": btc["usd"],
            "btc_24h": btc.get("usd_24h_change") or 0.0, "eth_price": eth.get("usd"),
            "eth_24h": eth.get("usd_24h_change"), "cats": cats}


# ---------- DefiLlama ----------
def tvl_change(name):
    pts = get(f"https://api.llama.fi/v2/historicalChainTvl/{quote(name)}")
    pts = [(int(p["date"]), p["tvl"]) for p in pts]
    return pct(pts[-1][1], value_days_ago(pts, 7, pts[-1][0]))


def stable_change(name):
    pts = get(f"https://stablecoins.llama.fi/stablecoincharts/{quote(name)}")
    pts = [(int(p["date"]), (p.get("totalCirculatingUSD") or {}).get("peggedUSD")) for p in pts]
    pts = [p for p in pts if p[1]]
    return pct(pts[-1][1], value_days_ago(pts, 7, pts[-1][0]))


def fetch_chains():
    all_chains = {c["name"].lower(): c for c in get("https://api.llama.fi/v2/chains")}
    stables = {c["name"].lower(): c for c in get("https://stablecoins.llama.fi/stablecoinchains")}
    rows = []
    for name, sym in CHAINS:
        c = all_chains.get(name.lower())
        if not c:
            print(f"  skip {name}: DefiLlamaに見つからない")
            continue
        print(f"  {name}")
        s = stables.get(name.lower()) or {}
        dex = safe(lambda: get(
            f"https://api.llama.fi/overview/dexs/{quote(name.lower())}",
            params={"excludeTotalDataChart": "true", "excludeTotalDataChartBreakdown": "true"}), {})
        rows.append({
            "name": name, "symbol": sym,
            "tvl": c.get("tvl"),
            "tvl_7d": safe(lambda: tvl_change(name)),
            "stable": (s.get("totalCirculatingUSD") or {}).get("peggedUSD"),
            "stable_7d": safe(lambda: stable_change(name)) if s else None,
            "dex_24h": dex.get("total24h"),
            # 直近7日の出来高 vs その前の7日
            "dex_7d": dex.get("change_7dover7d", dex.get("change_7d")),
        })
        time.sleep(0.4)
    return rows


# ---------- 先物（Binance → だめならOKX） ----------
def derivs_binance(symbols):
    prem = {p["symbol"]: p for p in get("https://fapi.binance.com/fapi/v1/premiumIndex")}
    out = {}
    for s in symbols:
        p = prem.get(f"{s}USDT")
        if not p:
            continue
        h = safe(lambda: get("https://fapi.binance.com/futures/data/openInterestHist",
                             params={"symbol": f"{s}USDT", "period": "1h", "limit": 25}))
        if not h:
            continue
        now, before = float(h[-1]["sumOpenInterestValue"]), float(h[0]["sumOpenInterestValue"])
        out[s] = {"funding": float(p["lastFundingRate"]), "oi": now, "oi_24h": pct(now, before)}
        time.sleep(0.2)
    return out


def derivs_okx(symbols):
    out = {}
    for s in symbols:
        fr = safe(lambda: get("https://www.okx.com/api/v5/public/funding-rate",
                              params={"instId": f"{s}-USDT-SWAP"})["data"])
        oi = safe(lambda: get("https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume",
                              params={"ccy": s, "period": "1H"})["data"])
        if not fr or not oi or len(oi) < 25:
            continue
        now, before = float(oi[0][1]), float(oi[24][1])  # 新しい順
        out[s] = {"funding": float(fr[0]["fundingRate"]), "oi": now, "oi_24h": pct(now, before)}
        time.sleep(0.2)
    return out


def fetch_derivs(symbols):
    for name, fn in (("Binance", derivs_binance), ("OKX", derivs_okx)):
        try:
            d = fn(symbols)
            if d:
                return name, d
            print(f"  {name}: データなし")
        except Exception as e:  # noqa: BLE001
            print(f"  {name} failed: {e}")
    return None, {}


# ---------- 価格トレンド（4時間足・30日分） ----------
def closes_binance(sym):
    k = get("https://data-api.binance.vision/api/v3/klines",
            params={"symbol": f"{sym}USDT", "interval": "4h", "limit": 181})
    return [float(x[4]) for x in k]  # 古い順


def closes_okx(sym):
    d = get("https://www.okx.com/api/v5/market/candles",
            params={"instId": f"{sym}-USDT", "bar": "4H", "limit": 181})["data"]
    return [float(x[4]) for x in reversed(d)]  # 新しい順で返るので反転


def trend_stats(closes):
    """4時間足の終値から、24h変化・7日線/30日線との差・トレンドを出す"""
    now = closes[-1]
    ma7 = sum(closes[-42:]) / len(closes[-42:])
    ma30 = sum(closes[-180:]) / len(closes[-180:]) if len(closes) >= 180 else None
    if ma30 is None:
        trend = None
    elif now > ma7 > ma30:
        trend = "up"
    elif now < ma7 < ma30:
        trend = "down"
    else:
        trend = "range"
    return {"price": now, "px_24h": pct(now, closes[-7]) if len(closes) >= 7 else None,
            "vs_ma7": pct(now, ma7), "vs_ma30": pct(now, ma30), "trend": trend}


def fetch_prices(symbols):
    sources = [["Binance", closes_binance, 0], ["OKX", closes_okx, 0]]
    out = {}
    for s in symbols:
        for src in sources:
            name, fn, fails = src
            if fails >= 3:
                continue  # 3回続けて失敗した取引所は以降使わない
            c = safe(lambda: fn(s))
            if c and len(c) >= 42:
                out[s] = trend_stats(c)
                src[2] = 0
                break
            src[2] += 1
        time.sleep(0.2)
    return out


# ---------- 長期チャート（日足） ----------
def day_of(ts):
    return int(ts) // 86400 * 86400


def daily_binance(sym):
    out, start = {}, LONG_START * 1000
    for _ in range(10):
        k = get("https://data-api.binance.vision/api/v3/klines",
                params={"symbol": f"{sym}USDT", "interval": "1d", "startTime": start, "limit": 1000})
        if not k:
            break
        out.update({day_of(x[0] / 1000): float(x[4]) for x in k})
        if len(k) < 1000:
            break
        start = k[-1][0] + 86400000
        time.sleep(0.3)
    return sorted(out.items())


def daily_okx(sym):
    out, after = {}, None
    for _ in range(40):  # 100日ずつ過去へ
        params = {"instId": f"{sym}-USDT", "bar": "1Dutc", "limit": 100}
        if after:
            params["after"] = after
        d = get("https://www.okx.com/api/v5/market/history-candles", params=params)["data"]
        if not d:
            break
        out.update({day_of(int(x[0]) / 1000): float(x[4]) for x in d})
        after = d[-1][0]
        if int(after) / 1000 < LONG_START:
            break
        time.sleep(0.25)
    return sorted(out.items())


def daily_prices(sym):
    for name, fn in (("Binance", daily_binance), ("OKX", daily_okx)):
        pts = safe(lambda: fn(sym))
        if pts and len(pts) > 30:
            print(f"  {sym}: {name} {len(pts)}日分")
            return [[t, round(v, 2)] for t, v in pts]
    return None


def fng_history():
    d = get("https://api.alternative.me/fng/", params={"limit": 0})["data"]
    return sorted([[day_of(x["timestamp"]), int(x["value"])] for x in d])


def update_longterm(lt, now_ts, dom, hist):
    """価格・恐怖強欲は1日1回全期間を取り直し、BTCドミナンスは自前で1日1点ずつ貯める"""
    if now_ts - lt.get("refreshed", 0) > LONG_REFRESH:
        print("長期チャート...")
        for key, sym in (("btc", "BTC"), ("eth", "ETH")):
            pts = daily_prices(sym)
            if pts:
                lt[key] = pts
        f = safe(fng_history)
        if f:
            lt["fng"] = f
        lt["refreshed"] = now_ts
    doms = lt.setdefault("dom", [])
    if not doms:  # 初回は溜まっている1時間ごとの履歴から日ごとの値を作る
        for snap in hist:
            if not doms or doms[-1][0] != day_of(snap["ts"]):
                doms.append([day_of(snap["ts"]), round(snap["btc_dom"], 3)])
    today = day_of(now_ts)
    if not doms or doms[-1][0] != today:
        doms.append([today, round(dom, 3)])
    return lt


def mock_longterm(now_ts):
    random.seed(11)
    lt = {"btc": [], "eth": [], "fng": [], "dom": [], "refreshed": now_ts}
    b, e = 1000.0, 8.0
    for t in range(day_of(LONG_START), day_of(now_ts) + 1, 86400):
        i = (t - LONG_START) / 86400
        b *= 1 + 0.0016 + 0.035 * math.sin(i / 90) * 0.1 + random.uniform(-0.035, 0.035)
        e *= 1 + 0.0019 + 0.045 * math.sin(i / 70) * 0.1 + random.uniform(-0.045, 0.045)
        lt["btc"].append([t, round(b, 2)])
        lt["eth"].append([t, round(e, 2)])
        if t >= 1517443200:  # 2018-02-01〜
            lt["fng"].append([t, max(3, min(97, round(50 + 30 * math.sin(i / 45) + random.uniform(-12, 12))))])
    for key, end in (("btc", 113400), ("eth", 4120)):  # サンプルの「今の価格」につながるように縮尺を合わせる
        k = end / lt[key][-1][1]
        lt[key] = [[t, round(v * k, 2)] for t, v in lt[key]]
    for t in range(day_of(now_ts) - 20 * 86400, day_of(now_ts) + 1, 86400):
        lt["dom"].append([t, round(57.1 + math.sin((day_of(now_ts) - t) / 86400 / 4) * 0.6, 3)])
    return lt


# ---------- 恐怖・強欲指数 ----------
FNG_JA = {"Extreme Fear": "極端な恐怖", "Fear": "恐怖", "Neutral": "中立",
          "Greed": "強欲", "Extreme Greed": "極端な強欲"}


def fng_text(v):
    if v <= 25:
        return "恐怖が強い状態です。逆張りの買い場になりやすい一方、下げ止まりを確認してからが安全です。"
    if v >= FNG_GREED:
        return "強欲が強い状態です。天井付近のことが多いので、新しいエントリーは慎重に。"
    if v < 46:
        return "恐怖寄りです。慌てた売りが出やすい一方、仕込み場になることもあります。"
    if v >= 55:
        return "強欲寄りです。上がりやすい地合いですが、過熱のサインも合わせて確認を。"
    return "中立圏です。個別のチェーンやセクターの動きを優先して見ましょう。"


def fetch_fng():
    d = get("https://api.alternative.me/fng/", params={"limit": 30})["data"]  # 新しい順
    vals = [int(x["value"]) for x in d]
    return {"value": vals[0], "label": FNG_JA.get(d[0]["value_classification"], d[0]["value_classification"]),
            "d1": vals[0] - vals[1] if len(vals) > 1 else None,
            "d7": vals[0] - vals[7] if len(vals) > 7 else None,
            "series": list(reversed(vals)), "text": fng_text(vals[0])}


# ---------- 注目トークン ----------
def token_label(t):
    tvl7, rel7 = t.get("tvl_7d"), t.get("rel_7d")
    if rel7 is not None and rel7 >= TOKEN_PUMP:
        return "hot", "急騰後・飛び乗り注意"
    if tvl7 is None or rel7 is None:
        return "flat", "判定材料不足"
    if tvl7 >= 3 and rel7 <= 0:
        return "early", "資金流入・価格は出遅れ"
    if tvl7 > 0 and rel7 > 0:
        return "good", "資金も価格も上向き"
    if tvl7 <= 0 and rel7 > 0:
        return "weak", "価格だけ先行"
    return "out", "弱い"


def rank_tokens(rows):
    ranks = {k: pct_rank([r.get(k) for r in rows]) for k in TOKEN_WEIGHTS}
    for i, r in enumerate(rows):
        tot = w = 0.0
        for k, wt in TOKEN_WEIGHTS.items():
            if ranks[k][i] is not None:
                tot += ranks[k][i] * wt
                w += wt
        r["score"] = round(tot / w * 100) if w else None
        r["label"], r["label_text"] = token_label(r)
    rows.sort(key=lambda r: -1 if r["score"] is None else r["score"], reverse=True)
    return rows[:TOKENS_PER_CHAIN]


def fetch_tokens(chain_names, native=None):
    """DefiLlamaのプロトコル一覧から各チェーンのトークンを拾い、CoinGeckoで価格を付ける"""
    picked = {n: {} for n in chain_names}  # chain -> gecko_id -> 候補
    # 「Aave V3」などの子プロトコルはトークンIDを親だけが持っていることがある
    parents = safe(lambda: get("https://api.llama.fi/lite/protocols2").get("parentProtocols"), []) or []
    parent_gid = {x.get("id"): x.get("gecko_id") for x in parents if x.get("gecko_id")}
    for p in get("https://api.llama.fi/protocols"):
        gid = p.get("gecko_id") or parent_gid.get(p.get("parentProtocol"))
        if not gid or p.get("category") in TOKEN_SKIP_CATS:
            continue
        total = p.get("tvl") or 0
        ct = p.get("chainTvls") or {}
        for n in chain_names:
            v = ct.get(n) or 0
            if v >= TOKEN_MIN_TVL and v >= total * TOKEN_SHARE:
                cur = picked[n].get(gid)
                if not cur or v > cur["tvl"]:  # V2/V3など同じトークンは大きい方を使う
                    picked[n][gid] = {"id": gid, "name": p.get("name"), "cat": p.get("category"),
                                      "tvl": v, "tvl_7d": p.get("change_7d")}
    ids = sorted({g for d in picked.values() for g in d} | {"bitcoin"})
    markets = {}
    for i in range(0, len(ids), 100):
        rows = cg("/coins/markets", {"vs_currency": "usd", "ids": ",".join(ids[i:i + 100]),
                                     "per_page": 250, "price_change_percentage": "24h,7d"})
        markets.update({m["id"]: m for m in rows})
        time.sleep(1)
    btc7 = (markets.get("bitcoin") or {}).get("price_change_percentage_7d_in_currency")
    out = {}
    native = native or {}
    for n, cands in picked.items():
        rows = []
        for gid, t in cands.items():
            m = markets.get(gid)
            if not m or (m.get("market_cap") or 0) < TOKEN_MIN_MCAP:
                continue
            if (m.get("symbol") or "").upper() == (native.get(n) or ""):
                continue  # チェーン自体の通貨はカード本体で見る
            px7 = m.get("price_change_percentage_7d_in_currency")
            if 0.95 <= (m.get("current_price") or 0) <= 1.05 and abs(px7 or 0) < 2:
                continue  # ステーブルコインっぽいものは除外
            t.update({"symbol": (m.get("symbol") or "").upper(), "mcap": m["market_cap"],
                      "px_24h": m.get("price_change_percentage_24h_in_currency"), "px_7d": px7,
                      "rel_7d": px7 - btc7 if (px7 is not None and btc7 is not None) else None,
                      "turnover": (m.get("total_volume") or 0) / m["market_cap"] * 100})
            rows.append(t)
        if rows:
            out[n] = rank_tokens(rows)
    print(f"  tokens: {sum(len(v) for v in out.values())} ({len(out)} chains)")
    return out


# ---------- OIと価格のズレ ----------
def oi_real(oi24, px24):
    """OIはドル建てなので、価格が上がるだけで増える。その分を除いた増減"""
    if oi24 is None or px24 is None:
        return None
    return ((1 + oi24 / 100) / (1 + px24 / 100) - 1) * 100


def oi_div(px, oi):
    if px is None or oi is None:
        return None, None
    if oi >= OI_SURGE and abs(px) < PX_MOVE:
        return "warn", "価格は動かずOIだけ急増（清算に注意）"
    if px >= PX_MOVE and oi >= OI_MOVE:
        return "good", "新しい買いが入って上昇中"
    if px >= PX_MOVE and oi <= -OI_MOVE:
        return "weak", "売りの買い戻しで上昇（続きにくい）"
    if px <= -PX_MOVE and oi >= OI_MOVE:
        return "warn", "下げながら売りが積み上がり中"
    if px <= -PX_MOVE and oi <= -OI_MOVE:
        return "flat", "ポジションの整理が進行中"
    return "flat", "目立ったズレなし"


# ---------- スコア ----------
def pct_rank(values):
    idx = sorted([i for i, v in enumerate(values) if v is not None], key=lambda i: values[i])
    out = [None] * len(values)
    n = len(idx)
    for r, i in enumerate(idx):
        out[i] = r / (n - 1) if n > 1 else 0.5
    return out


def label(r):
    fr, oi = r.get("funding"), r.get("oi_24h")
    flows = [x for x in (r.get("stable_7d"), r.get("tvl_7d"), r.get("dex_7d")) if x is not None]
    if (fr is not None and fr >= FR_HOT) or (oi is not None and oi >= OI_HOT):
        return "hot", "過熱気味"
    if flows and sum(x > 0 for x in flows) >= 2 and (fr is None or fr < FR_CALM):
        return "early", "静かに流入中"
    if flows and all(x < 0 for x in flows):
        return "out", "資金流出"
    return "flat", "様子見"


def score_chains(rows):
    ranks = {k: pct_rank([r.get(k) for r in rows]) for k in WEIGHTS}
    for i, r in enumerate(rows):
        tot = w = 0.0
        for k, wt in WEIGHTS.items():
            if ranks[k][i] is not None:
                tot += ranks[k][i] * wt
                w += wt
        s = tot / w * 100 if w else None
        if s is not None and (r.get("funding") or 0) >= FR_HOT:
            s -= 15  # 資金流入があってもレバが混みすぎなら減点
        r["score"] = None if s is None else round(max(0, min(100, s)))
        r["label"], r["label_text"] = label(r)
        r["oi_real"] = oi_real(r.get("oi_24h"), r.get("px_24h"))
        r["div"], r["div_text"] = oi_div(r.get("px_24h"), r["oi_real"])
    rows.sort(key=lambda r: -1 if r["score"] is None else r["score"], reverse=True)


def entry_check(r, fng):
    """資金流入 + 上昇トレンド + OIに危ないズレなし + 相場全体が強欲すぎない"""
    ok = (r["label"] == "early" and r.get("trend") == "up" and r.get("div") not in ("warn", "weak")
          and (r.get("score") or 0) >= ENTRY_MIN_SCORE and (fng is None or fng["value"] < FNG_GREED))
    r["entry"] = bool(ok)


def build_sectors(m, hist, now_ts):
    cats = [c for c in m["cats"]
            if (c.get("market_cap") or 0) >= MIN_CAT_MCAP and c.get("market_cap_change_24h") is not None]
    cats = sorted(cats, key=lambda c: -c["market_cap"])[:MAX_CATS]
    btc7 = pct(m["btc_price"], hist_value(hist, lambda s: s["btc_price"], 7, now_ts))
    out = []
    for c in cats:
        cid = c["id"]
        rel24 = c["market_cap_change_24h"] - m["btc_24h"]
        c7 = pct(c["market_cap"], hist_value(hist, lambda s: s["cats"][cid][0], 7, now_ts))
        streak = 0
        if rel24 > 0:
            streak = 1
            for s in reversed(hist):
                v = (s.get("cats", {}).get(cid) or [None, None])[1]
                if v is None or v <= 0:
                    break
                streak += 1
        out.append({
            "id": cid, "name": c["name"], "mcap": c["market_cap"],
            "chg_24h": c["market_cap_change_24h"], "rel_24h": rel24,
            "rel_7d": (c7 - btc7) if (c7 is not None and btc7 is not None) else None,
            "streak": streak, "top": (c.get("top_3_coins_id") or [])[:3],
        })
    out.sort(key=lambda x: x["rel_24h"], reverse=True)
    return out


def phase_text(dom7):
    if dom7 is None:
        return "7日分の履歴が溜まると、BTCシェアの流れが表示されます。"
    if dom7 <= -1.0:
        return "BTCのシェアが下がっています。アルトに資金が回り始めている可能性があります。"
    if dom7 >= 1.0:
        return "BTCのシェアが上がっています。資金はBTCに集まり気味です。"
    return "BTCのシェアはほぼ横ばいです。"


# ---------- サンプルデータ ----------
def mock_all(now_ts):
    random.seed(7)
    cats_def = [("solana-ecosystem", "Solana Ecosystem", 9e10), ("meme-token", "Meme", 6e10),
                ("artificial-intelligence", "Artificial Intelligence (AI)", 3e10),
                ("layer-2", "Layer 2 (L2)", 2e10), ("real-world-assets-rwa", "Real World Assets (RWA)", 2.5e10),
                ("sui-ecosystem", "Sui Ecosystem", 1.5e10), ("defi", "Decentralized Finance (DeFi)", 8e10),
                ("gaming", "Gaming (GameFi)", 1e10), ("xrp-ledger-ecosystem", "XRP Ledger Ecosystem", 1.4e11),
                ("liquid-staking", "Liquid Staking", 5e10), ("depin", "DePIN", 1.2e10),
                ("bnb-chain-ecosystem", "BNB Chain Ecosystem", 1.1e11)]
    hist = []
    for h in range(72, 0, -1):
        ts = now_ts - h * 3600
        hist.append({"ts": ts, "btc_dom": 58.5 - (72 - h) * 0.02 + math.sin(h / 5) * 0.1,
                     "btc_price": 112000 + math.sin(h / 9) * 1500,
                     "cats": {cid: [mc * (1 - h * 0.0008), random.uniform(-1, 3) if cid == "solana-ecosystem"
                                    else random.uniform(-2, 2)] for cid, _, mc in cats_def},
                     "chains": {n: random.randint(20, 80) for n, _ in CHAINS[:10]}})
    market = {"btc_dom": 57.1, "btc_price": 113400, "btc_24h": 1.2, "eth_price": 4120, "eth_24h": 2.3, "cats": [
        {"id": cid, "name": nm, "market_cap": mc, "market_cap_change_24h": random.uniform(-4, 9),
         "top_3_coins_id": ["coin-a", "coin-b", "coin-c"]} for cid, nm, mc in cats_def]}
    chains = [{"name": n, "symbol": s, "tvl": random.uniform(5e8, 6e10), "tvl_7d": random.uniform(-8, 12),
               "stable": random.uniform(3e8, 8e10), "stable_7d": random.uniform(-5, 10),
               "dex_24h": random.uniform(5e7, 3e9), "dex_7d": random.uniform(-30, 60)} for n, s in CHAINS[:12]]
    derivs = {s: {"funding": random.choice([0.00005, 0.0001, 0.00015, 0.0003, 0.0007]),
                  "oi": random.uniform(1e8, 2e10), "oi_24h": random.uniform(-10, 30)}
              for _, s in CHAINS if s}
    prices = {}
    for _, s in CHAINS:
        if not s:
            continue
        drift = random.uniform(-0.004, 0.005)
        c, v = [], 100.0
        for _ in range(181):
            v *= 1 + drift + random.uniform(-0.02, 0.02)
            c.append(v)
        prices[s] = trend_stats(c)
    fv = [max(5, min(95, round(50 + 25 * math.sin(i / 6) + random.uniform(-5, 5)))) for i in range(30)]
    fv_label = next(t for lim, t in ((25, "極端な恐怖"), (46, "恐怖"), (54, "中立"), (75, "強欲"), (101, "極端な強欲"))
                    if fv[-1] < lim)
    fng = {"value": fv[-1], "label": fv_label, "d1": fv[-1] - fv[-2], "d7": fv[-1] - fv[-8],
           "series": fv, "text": fng_text(fv[-1])}
    cats = ["Dexs", "Lending", "Liquid Staking", "Derivatives", "Yield", "CDP"]
    tokens = {}
    for c in chains:
        rows = [{"id": f"tok-{c['name']}-{i}", "name": f"{c['name'].split()[0]} {cats[i % 6]} {i + 1}",
                 "symbol": f"{c['name'][:2].upper()}T{i + 1}", "cat": cats[i % 6],
                 "tvl": random.uniform(5e6, 3e9), "tvl_7d": random.uniform(-15, 25),
                 "mcap": random.uniform(1e7, 5e9), "px_24h": random.uniform(-8, 12),
                 "px_7d": random.uniform(-20, 45), "rel_7d": random.uniform(-20, 50),
                 "turnover": random.uniform(1, 40)} for i in range(8)]
        tokens[c["name"]] = rank_tokens(rows)
    return hist, market, chains, ("Mock", derivs), prices, fng, tokens


# ---------- メイン ----------
def main():
    now_ts = int(time.time())
    warnings = []
    if MOCK:
        hist, market, chains, (dsrc, derivs), prices, fng, tokens = mock_all(now_ts)
    else:
        hist = load(HIST_PATH, [])
        if not CG_KEY:
            warnings.append("COINGECKO_API_KEY が未設定です。GitHubのSecretsに登録してください。")
        print("CoinGecko...")
        market = fetch_market()
        print("DefiLlama...")
        chains = safe(fetch_chains, [])
        if not chains:
            warnings.append("DefiLlamaからチェーンのデータを取れませんでした。")
        print("先物...")
        dsrc, derivs = fetch_derivs([r["symbol"] for r in chains if r["symbol"]])
        if not derivs:
            warnings.append("取引所の先物データを取れませんでした（地域ブロックの可能性）。FRとOIなしで計算しています。")
        print("価格...")
        prices = fetch_prices([r["symbol"] for r in chains if r["symbol"]])
        if not prices:
            warnings.append("価格データを取れませんでした。トレンドとOIのズレは表示されません。")
        print("恐怖・強欲指数...")
        fng = safe(fetch_fng)
        print("注目トークン...")
        tokens = safe(lambda: fetch_tokens([r["name"] for r in chains],
                                           {r["name"]: r["symbol"] for r in chains if r["symbol"]}), {})

    for r in chains:
        d = derivs.get(r["symbol"] or "", {})
        r.update({"funding": d.get("funding"), "oi": d.get("oi"), "oi_24h": d.get("oi_24h")})
        p = prices.get(r["symbol"] or "", {})
        r.update({k: p.get(k) for k in ("price", "px_24h", "vs_ma7", "vs_ma30", "trend")})
    score_chains(chains)
    for r in chains:
        entry_check(r, fng)
        r["tokens"] = tokens.get(r["name"], [])
    for i, r in enumerate([r for r in chains if r["entry"]]):  # chainsは温度の高い順
        r["entry"] = i < ENTRY_MAX

    # スコアの推移（48時間）
    for r in chains:
        r["score_series"] = [s.get("chains", {}).get(r["name"]) for s in hist[-47:]] + [r["score"]]

    sectors = build_sectors(market, hist, now_ts)
    dom = market["btc_dom"]
    dom24 = hist_value(hist, lambda s: s["btc_dom"], 1, now_ts)
    dom7 = hist_value(hist, lambda s: s["btc_dom"], 7, now_ts)
    dom7_delta = dom - dom7 if dom7 is not None else None

    snap = {"ts": now_ts, "btc_dom": dom, "btc_price": market["btc_price"],
            "cats": {s["id"]: [s["mcap"], round(s["rel_24h"], 2)] for s in sectors},
            "chains": {r["name"]: r["score"] for r in chains}}
    hist = (hist + [snap])[-HIST_MAX:]

    data = {
        "updated": datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
        "mock": MOCK,
        "btc": {"dom": dom, "dom_24h": dom - dom24 if dom24 is not None else None,
                "dom_7d": dom7_delta, "price": market["btc_price"], "chg_24h": market["btc_24h"],
                "eth_price": market.get("eth_price"), "eth_24h": market.get("eth_24h"),
                "phase": phase_text(dom7_delta)},
        "dom_series": [[s["ts"], round(s["btc_dom"], 3)] for s in hist[-24 * 7:]],
        "fng": fng,
        "chains": chains,
        "sectors": sectors,
        "deriv_source": dsrc,
        "warnings": warnings,
    }
    DOCS.mkdir(exist_ok=True)
    DATA_PATH.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    if MOCK:
        lt = mock_longterm(now_ts)
    else:
        HIST_PATH.write_text(json.dumps(hist, separators=(",", ":")))
        lt = update_longterm(load(LONG_PATH, {}), now_ts, dom, hist)
    LONG_PATH.write_text(json.dumps(lt, separators=(",", ":")))
    print(f"done: {DATA_PATH.name} / chains={len(chains)} sectors={len(sectors)} deriv={dsrc}")


if __name__ == "__main__":
    main()
