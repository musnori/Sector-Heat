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
# 取引できる銘柄（Hyperliquid の無期限先物 ＋ Ostium）
OSTIUM_FALLBACK = ["BTC", "ETH", "SOL"]  # Ostium のAPIが取れなかったときに使う
# Ostium は株・指数・商品も扱っていて、SPX（S&P500）や META（Meta株）が同じ記号の仮想通貨と混ざるため、
# 仮想通貨として扱うのはこのリストにあるものだけ。Ostium で取引できる仮想通貨が増えたら足してOK
OSTIUM_CRYPTO = {"BTC", "ETH", "SOL", "XRP", "LINK", "HYPE", "BNB", "ADA", "TRX", "DOGE", "SUI", "AVAX",
                 "LTC", "DOT", "TON", "NEAR", "BCH", "XLM"}
CAT_PATH = DOCS / "catcache.json"         # カテゴリの構成銘柄（CoinGecko）のキャッシュ
CAT_MAX_AGE = 12 * 3600                   # セクターの構成銘柄は12時間ごとに取り直す
ECO_MAX_AGE = 48 * 3600                   # チェーンのエコシステムの構成銘柄は48時間ごと
CAT_BUDGET = 10                           # 1回の更新で構成銘柄を取りに行く最大回数（CoinGeckoの無料枠対策）
# 注目トークン（各チェーンで取引できるもの）
TOKENS_PER_CHAIN = 8
TOKEN_MIN_TVL = 1e6     # そのチェーン上のTVLがこれ未満のプロトコルは除外
TOKEN_SHARE = 0.3       # TVLの3割以上がそのチェーンにあれば「そのチェーンのトークン」扱い
TOKEN_EXCLUDE = {"BTC", "ETH", "USDC", "USDT", "USDE", "DAI"}  # エコシステムに入っていても主要通貨・ステーブルは除く
ECO_MULTI = 3   # これ以上のチェーンのエコシステムに入っている銘柄はブリッジ版とみなし、Ethereum以外では出さない
TOKEN_SKIP_CATS = {"CEX", "Chain", "Bridge", "Canonical Bridge", "Cross Chain Bridge", "Bridge Aggregators",
                   "Liquid Staking", "Liquid Restaking", "Restaking", "Restaked BTC", "Indexes", "Basis Trading"}
# 「<チェーン> Ecosystem」以外の名前のCoinGeckoカテゴリ
CHAIN_ECO = {"BSC": "BNB Chain Ecosystem", "Hyperliquid L1": "Hyperliquid Ecosystem",
             "OP Mainnet": "Optimism Ecosystem", "XRPL": "XRP Ledger Ecosystem",
             "Near": "Near Protocol Ecosystem", "TON": "TON Ecosystem"}
# セクターの作戦
PLAN_UP = 4             # 資金が向かっているセクターを何個出すか
PLAN_DOWN = 2           # 資金が抜けているセクターを何個出すか
PLAN_COINS = 8          # 1セクターあたり表示する取引できる銘柄の数
SECTOR_SKIP = ("stablecoin", "tokenized", "usd", "gold", "treasur", "money-market", "commodit", "fiat",
               "made-in", "alleged", "portfolio", "launchpool", "launchpad", "hodler", "binance-alpha", "ido",
               "yzi-labs", "exchange-based", "centralized-exchange", "wallets", "crypto-card", "neobank")


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


def post(url, body, tries=3):
    last = None
    for i in range(tries):
        try:
            r = requests.post(url, json=body, headers=UA, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last = e
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


# ---------- 取引できる銘柄 ----------
def hl_symbol(name):
    """Hyperliquid の kPEPE（1000枚単位）などを PEPE にそろえる"""
    return name[1:] if name.startswith("k") and len(name) > 2 and name[1:].isupper() else name


def fetch_hyperliquid():
    meta, ctxs = post("https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs"})
    out = {}
    for u, c in zip(meta["universe"], ctxs):
        if u.get("isDelisted"):
            continue
        mark, prev = float(c.get("markPx") or 0), float(c.get("prevDayPx") or 0)
        out[hl_symbol(u["name"]).upper()] = {
            "hl": u["name"], "chg_24h": pct(mark, prev),
            "fr8h": float(c.get("funding") or 0) * 8,  # HLは1時間ごとの資金調達率。他と比べやすいよう8時間換算
            "vol": float(c.get("dayNtlVlm") or 0), "oi_usd": float(c.get("openInterest") or 0) * mark}
    return out


def fetch_ostium():
    d = get("https://metadata-backend.ostium.io/PricePublish/latest-prices")
    return sorted({(x.get("from") or "").upper() for x in d if x.get("from")})


def fetch_tradable():
    """{シンボル: {venues:[...], chg_24h, fr8h, vol, oi_usd}}。値動きはHyperliquidのものを使う"""
    out = {}
    for sym, v in (safe(fetch_hyperliquid, {}) or {}).items():
        out[sym] = {**v, "venues": ["HL"]}
    ost = [x for x in (safe(fetch_ostium) or OSTIUM_FALLBACK) if x in OSTIUM_CRYPTO]
    for sym in ost:
        out.setdefault(sym, {"venues": []})["venues"].append("Ostium")
    print(f"  tradable: HL {sum('HL' in v['venues'] for v in out.values())} / Ostium {len(ost)}")
    return out


def coin_row(sym, t, **extra):
    return {"symbol": sym, "venues": t["venues"], "chg_24h": t.get("chg_24h"), "fr8h": t.get("fr8h"),
            "vol": t.get("vol"), **extra}


# ---------- カテゴリの構成銘柄（キャッシュ付き） ----------
class CatCache:
    def __init__(self, path, now_ts, budget):
        self.path, self.now, self.budget = path, now_ts, budget
        self.d = load(path, {})

    def members(self, cid, max_age=CAT_MAX_AGE):
        """{シンボル: 7日変化%}。古ければ取り直す（1回の更新で budget 回まで）"""
        e = self.d.get(cid)
        if e and self.now - e["ts"] < max_age:
            return e["m"]
        if self.budget <= 0:
            return e["m"] if e else None
        self.budget -= 1
        rows = safe(lambda: cg("/coins/markets", {"vs_currency": "usd", "category": cid, "per_page": 150,
                                                  "order": "market_cap_desc", "price_change_percentage": "7d"}))
        time.sleep(1.5)
        if rows is None:
            return e["m"] if e else None
        m = {}
        for r in rows:
            sym = (r.get("symbol") or "").upper()
            if sym and sym not in m:  # 同じシンボルは時価総額の大きい方
                m[sym] = r.get("price_change_percentage_7d_in_currency")
        self.d[cid] = {"ts": self.now, "m": m}
        return m

    def save(self):
        self.path.write_text(json.dumps(self.d, separators=(",", ":")))


# ---------- セクターの作戦 ----------
def median(xs):
    xs = sorted(x for x in xs if x is not None)
    return xs[len(xs) // 2] if xs else None


def sector_phase(s, med7):
    if s["rel_24h"] >= 10 or (med7 or 0) >= 40:
        return "hot", "過熱", "新しく買うのは控えめに。持っているなら一部利確を検討。"
    if s["streak_h"] >= 12 or (med7 or 0) >= 10:
        return "cont", "継続", "流れは続いている。先頭の銘柄を追いかけるより、押し目か出遅れ銘柄を待つ。"
    return "early", "初動", "強くなり始めたところ。出遅れ候補を小さく試し、セクターがBTCより弱くなったら撤退。"


def build_plan(sectors, tradable, cache):
    """強いセクター・弱いセクターごとに、HyperliquidかOstiumで取引できる銘柄を並べる"""
    cands = [s for s in sectors if not any(k in s["id"] for k in SECTOR_SKIP)]
    up, down = [], []
    for side, pool, n in (("up", sorted(cands, key=lambda x: -x["rel_24h"]), PLAN_UP),
                          ("down", sorted(cands, key=lambda x: x["rel_24h"]), PLAN_DOWN)):
        out = up if side == "up" else down
        for s in pool:
            if len(out) >= n or (side == "up" and s["rel_24h"] <= 0) or (side == "down" and s["rel_24h"] >= 0):
                break
            m = cache.members(s["id"])
            if not m:
                continue
            coins = [coin_row(sym, tradable[sym], px_7d=m[sym]) for sym in m if sym in tradable]
            if len(coins) < 2:
                continue
            coins.sort(key=lambda c: -(c["vol"] or 0))  # 出来高の多い順（入りやすさ）
            coins = coins[:PLAN_COINS]
            med7 = median([c["px_7d"] for c in coins])
            chgs = [c["chg_24h"] for c in coins if c["chg_24h"] is not None]
            med24 = median(chgs)
            if side == "up":
                code, ph, act = sector_phase(s, med7)
                lead = max(coins, key=lambda c: c["chg_24h"] if c["chg_24h"] is not None else -1e9)
                for c in coins:
                    if c is lead and (c["chg_24h"] or 0) > 0:
                        c["tag"] = "先頭"
                    elif (c["chg_24h"] is not None and med24 is not None and c["chg_24h"] < med24
                          and c["chg_24h"] > -3 and (c["fr8h"] is None or c["fr8h"] < FR_CALM)):
                        c["tag"] = "出遅れ候補"
            else:
                code, ph = "down", "資金流出"
                act = "ロングは避ける。弱い銘柄はショート候補（ロングが資金調達料を払っている銘柄は特に）。"
                # 下がっている銘柄のうち、ロングが資金調達料を払っている（FR>=0）ものを優先
                weak = sorted([c for c in coins if (c["chg_24h"] or 0) < 0],
                              key=lambda c: ((c["fr8h"] or 0) < 0, c["chg_24h"]))
                for c in weak[:2]:
                    c["tag"] = "ショート候補"
            out.append({"id": s["id"], "name": s["name"], "rel_24h": s["rel_24h"], "rel_7d": s["rel_7d"],
                        "streak_h": s["streak_h"], "med_7d": med7, "phase": code, "phase_text": ph,
                        "action": act, "coins": coins})
    print(f"  plan: up={len(up)} down={len(down)}")
    return {"up": up, "down": down}


# ---------- 注目トークン（各チェーンで取引できるもの） ----------
def eco_category(chain, cats):
    want = (CHAIN_ECO.get(chain) or f"{chain} Ecosystem").lower()
    for c in cats:
        if (c.get("name") or "").lower() == want:
            return c["id"]
    return None


def fetch_tokens(chain_names, native, tradable, cache, cats):
    """DefiLlamaでそのチェーンのDeFiトークンを、CoinGeckoのエコシステムでそれ以外も拾い、取引できるものだけ残す"""
    picked = {n: {} for n in chain_names}
    parents = safe(lambda: get("https://api.llama.fi/lite/protocols2").get("parentProtocols"), []) or []
    parent_sym = {x.get("id"): x.get("symbol") for x in parents}
    for p in safe(lambda: get("https://api.llama.fi/protocols"), []) or []:
        sym = (p.get("symbol") or "").upper()
        if sym in ("", "-"):
            sym = (parent_sym.get(p.get("parentProtocol")) or "").upper()
        if sym not in tradable or p.get("category") in TOKEN_SKIP_CATS:
            continue
        total = p.get("tvl") or 0
        ct = p.get("chainTvls") or {}
        for n in chain_names:
            v = ct.get(n) or 0
            if v >= TOKEN_MIN_TVL and v >= total * TOKEN_SHARE:
                cur = picked[n].get(sym)
                if not cur or v > cur["tvl"]:  # V2/V3など同じトークンは大きい方
                    picked[n][sym] = {"name": p.get("name"), "cat": p.get("category"), "tvl": v,
                                      "tvl_7d": p.get("change_7d")}
    eco = {}
    for n in chain_names:
        cid = eco_category(n, cats)
        eco[n] = (cache.members(cid, ECO_MAX_AGE) if cid else None) or {}
    # SOLやUNIのブリッジ版は色々なチェーンのエコシステムに入っているので、そのチェーンの銘柄とは言いにくい
    seen = {}
    for m in eco.values():
        for sym in m:
            seen[sym] = seen.get(sym, 0) + 1
    natives = {x for x in (native or {}).values() if x}
    out = {}
    for n in chain_names:
        rows = {sym: coin_row(sym, tradable[sym], **t) for sym, t in picked[n].items()}
        for sym, px7 in eco[n].items():
            if sym in TOKEN_EXCLUDE or (sym in natives and sym != (native or {}).get(n)):
                continue  # 他のチェーン自体の通貨（SuiエコシステムのSOLなど）
            if seen.get(sym, 0) >= ECO_MULTI and n != "Ethereum" and sym not in rows:
                continue
            if sym in tradable and sym not in rows:
                rows[sym] = coin_row(sym, tradable[sym], name=None, cat="エコシステム", px_7d=px7)
            elif sym in rows:
                rows[sym]["px_7d"] = px7
        rows.pop((native or {}).get(n) or "", None)  # チェーン自体の通貨はカード本体で見る
        rows = sorted(rows.values(), key=lambda r: -(r["vol"] or 0))[:TOKENS_PER_CHAIN]
        if rows:
            out[n] = rows
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
        streak_h, since = 0, None
        if rel24 > 0:  # 何時間前からBTCを上回り続けているか（更新間隔がばらついても時間で数える）
            since = now_ts
            for s in reversed(hist):
                v = (s.get("cats", {}).get(cid) or [None, None])[1]
                if v is None or v <= 0:
                    break
                since = s["ts"]
            streak_h = round((now_ts - since) / 3600)
        out.append({
            "id": cid, "name": c["name"], "mcap": c["market_cap"],
            "chg_24h": c["market_cap_change_24h"], "rel_24h": rel24,
            "rel_7d": (c7 - btc7) if (c7 is not None and btc7 is not None) else None,
            "streak_h": streak_h, "top": (c.get("top_3_coins_id") or [])[:3],
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
    syms = ["BTC", "ETH", "SOL", "XRP", "DOGE", "WIF", "BONK", "PEPE", "POPCAT", "FARTCOIN", "JUP", "RAY", "PYTH",
            "JTO", "RENDER", "TAO", "FET", "VIRTUAL", "AI16Z", "ONDO", "PENDLE", "AAVE", "UNI", "MORPHO", "AERO",
            "ZRO", "STRK", "ZK", "OP", "ARB", "CETUS", "DEEP", "NAVX", "HYPE", "PURR", "LINK", "ENA", "ETHFI"]
    tradable = {x: {"venues": ["HL"] + (["Ostium"] if x in ("BTC", "ETH", "SOL", "XRP", "LINK") else []),
                    "chg_24h": random.uniform(-9, 14), "fr8h": random.choice([-0.0001, 0.00005, 0.0001, 0.0003, 0.0008]),
                    "vol": random.uniform(2e6, 8e8)} for x in syms}
    kinds = ["Dexs", "Lending", "Derivatives", "Yield", "エコシステム"]
    tokens = {}
    for c in chains:
        tokens[c["name"]] = sorted([coin_row(x, tradable[x], name=None, cat=random.choice(kinds),
                                             tvl_7d=random.uniform(-10, 20), px_7d=random.uniform(-20, 40))
                                    for x in random.sample(syms[2:], 5)], key=lambda r: -r["vol"])
    return hist, market, chains, ("Mock", derivs), prices, fng, tokens, tradable


class MockCache:
    def __init__(self, tradable):
        self.syms = list(tradable)

    def members(self, cid):
        rnd = random.Random(cid)
        return {x: rnd.uniform(-20, 45) for x in rnd.sample(self.syms, 7)}

    def save(self):
        pass


# ---------- メイン ----------
def main():
    now_ts = int(time.time())
    warnings = []
    if MOCK:
        hist, market, chains, (dsrc, derivs), prices, fng, tokens, tradable = mock_all(now_ts)
        cache = MockCache(tradable)
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
        print("取引できる銘柄...")
        tradable = fetch_tradable()
        if not any("HL" in v["venues"] for v in tradable.values()):
            warnings.append("Hyperliquidの銘柄一覧を取れませんでした。取引できる銘柄の表示が少なくなっています。")
        cache = CatCache(CAT_PATH, now_ts, CAT_BUDGET)

    for r in chains:
        d = derivs.get(r["symbol"] or "", {})
        r.update({"funding": d.get("funding"), "oi": d.get("oi"), "oi_24h": d.get("oi_24h")})
        p = prices.get(r["symbol"] or "", {})
        r.update({k: p.get(k) for k in ("price", "px_24h", "vs_ma7", "vs_ma30", "trend")})
    score_chains(chains)
    for r in chains:
        entry_check(r, fng)
    for i, r in enumerate([r for r in chains if r["entry"]]):  # chainsは温度の高い順
        r["entry"] = i < ENTRY_MAX

    # スコアの推移（48時間）
    for r in chains:
        r["score_series"] = [s.get("chains", {}).get(r["name"]) for s in hist[-47:]] + [r["score"]]

    sectors = build_sectors(market, hist, now_ts)
    print("セクターの作戦...")
    plan = safe(lambda: build_plan(sectors, tradable, cache), {"up": [], "down": []})
    if not MOCK:  # 作戦の方が大事なので、CoinGeckoの呼び出し枠は作戦→チェーンのトークンの順に使う
        print("取引できるトークン...")
        tokens = safe(lambda: fetch_tokens([r["name"] for r in chains],
                                           {r["name"]: r["symbol"] for r in chains if r["symbol"]},
                                           tradable, cache, market["cats"]), {}) or {}
    for r in chains:
        r["tokens"] = tokens.get(r["name"], [])
    cache.save()
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
        "plan": plan,
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
