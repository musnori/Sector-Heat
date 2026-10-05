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
META_PATH = ROOT / "cache" / "coin_meta.json"  # 上位銘柄がどのセクターに属するか（CoinGecko）のキャッシュ
HIST_MAX = 24 * 30  # 30日分（1時間ごと）
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
    ("Cardano", "ADA"), ("Hedera", "HBAR"),
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
# いまの見立て（時価総額上位の銘柄ごとの状態）
TOP_RANK = 20          # 監視する銘柄数。HyperliquidかOstiumで取引できる銘柄を時価総額の上からこの数（BTC・ステーブル・ラップ系は除く）
META_REFRESH_DAYS = 7  # 銘柄のカテゴリ情報はめったに変わらないので週1で取り直す
META_PER_RUN = 10      # 1回の実行で取り直す銘柄数の上限（CoinGeckoの無料枠を守る）
# ステーブル・ラップ系など「買う対象」ではないもの
NOT_TRADABLE_CAT = ["stablecoin", "wrapped", "bridged", "liquid staking token", "liquid staked",
                    "tokenized gold", "tokenized commodit", "yield-bearing"]
NOT_TRADABLE_SYM = {"USDT", "USDC", "DAI", "USDE", "FDUSD", "PYUSD", "USDS", "USD1", "TUSD", "USDD",
                    "WBTC", "WETH", "STETH", "WSTETH", "WEETH", "CBBTC", "WBETH", "SUSDE", "BSC-USD",
                    "XAUT", "PAXG", "BUIDL", "USDTB", "RLUSD", "USDF", "JITOSOL", "RETH", "LBTC"}
# 銘柄の関連セクターとして見ないカテゴリ（資産運用会社のポートフォリオや、広すぎるもの）
EXCLUDE_CAT = ["portfolio", "holdings", "index", "launchpool", "launchpad", "alleged",
               "made in", "stablecoin", "wrapped", "bridged", "tokenized", "binance hodler",
               "exchange-based", "yzi labs", "world liberty",
               # 広すぎて「どこに資金が回っているか」が分からないカテゴリ
               "layer 1 (l1)", "smart contract platform", "proof of work", "proof of stake",
               "layer 0", "ethereum ecosystem", "coinbase", "gmci", "fan token"]
COIN_ENTRY_MAX = 3     # 銘柄の「条件そろい」は並び順で最大この件数まで
HOT_MOVE = 10.0        # 24時間でこれ以上（%）上がったら「過熱」
EARLY_MOVE = 2.0       # 下落・もみ合いから7日線を上抜けて、24時間でこれ以上上がったら「初動」
DIP_MOVE = -2.0        # 上昇トレンド中に24時間でこれ以下なら「押し目」
FLOW_MOVE = 2.0        # 「資金の流れ」で、24時間でこれ以上動いた銘柄を「入っている／抜けている」とする
SECTOR_CHIPS = 4       # 詳細に出す関連セクターの数
STATUS = {  # 銘柄ごとの「いまの状態」（表示名, 並び順）
    "strong": ("強い", 0),
    "early":  ("初動", 1),
    "dip":    ("押し目", 2),
    "hot":    ("過熱", 3),
    "flat":   ("様子見", 4),
    "weak":   ("弱い", 5),
}


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


def quiet(getter, s):
    try:
        return getter(s)
    except (KeyError, IndexError, TypeError):
        return None


def hist_value(hist, getter, days, now_ts):
    return value_days_ago([(s["ts"], quiet(getter, s)) for s in hist], days, now_ts)


# ---------- CoinGecko ----------
def cg(path, params=None):
    headers = {"x-cg-demo-api-key": CG_KEY} if CG_KEY else {}
    return get(CG_BASE + path, headers=headers, params=params)


def fetch_market():
    """1時間あたり2回: カテゴリ一覧、上位銘柄（BTCの値動きもここから）"""
    cats = cg("/coins/categories")
    top = cg("/coins/markets", {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 60,
                                "page": 1, "price_change_percentage": "24h,7d"})
    btc = next(c for c in top if c["id"] == "bitcoin")
    return {"btc_price": btc["current_price"],
            "btc_24h": btc.get("price_change_percentage_24h_in_currency") or 0.0,
            "btc_7d": btc.get("price_change_percentage_7d_in_currency"),
            "cats": cats, "top": top}


def refresh_meta(top, meta, now_ts):
    """銘柄ごとのカテゴリ（どのセクターに属するか）をキャッシュ。古いものだけ取り直す"""
    stale = [c["id"] for c in top if (c.get("market_cap_rank") or 999) <= TOP_RANK * 2 + 10
             and now_ts - meta.get(c["id"], {}).get("ts", 0) > META_REFRESH_DAYS * 86400]
    for cid in stale[:META_PER_RUN]:
        d = safe(lambda: cg(f"/coins/{cid}", {"localization": "false", "tickers": "false",
                                              "market_data": "false", "community_data": "false",
                                              "developer_data": "false"}))
        if d:
            meta[cid] = {"cats": [x for x in (d.get("categories") or []) if x], "ts": now_ts}
            print(f"  meta: {cid} {len(meta[cid]['cats'])} categories")
        time.sleep(2.5)
    return meta


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


# ---------- 先物（Hyperliquid → 足りない分は Binance → OKX） ----------
def oi_24h_ago(hist, sym, now_ts):
    """Hyperliquidは今の建玉しか返さないので、毎時保存した値と比べる"""
    return hist_value(hist, lambda x: x["oi"][sym], 1, now_ts)


def derivs_from_hl(tradable, symbols, hist, now_ts):
    out = {}
    for s in symbols:
        t = tradable.get(s) or {}
        if t.get("oi_usd") is not None:
            out[s] = {"funding": t["fr8h"], "oi": t["oi_usd"], "oi_24h": pct(t["oi_usd"], oi_24h_ago(hist, s, now_ts))}
    return out


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


# ---------- 価格トレンドとRSI（4時間足・1時間足） ----------
RSI_LEN = 14   # RSIの期間
BARS = {"4h": ("4h", "4H", 31 * 86400), "1h": ("1h", "1H", 7 * 86400)}  # Binance, OKX, Hyperliquidで取る期間


def closes_binance(sym, iv):
    k = get("https://data-api.binance.vision/api/v3/klines",
            params={"symbol": f"{sym}USDT", "interval": BARS[iv][0], "limit": 181})
    return [float(x[4]) for x in k]  # 古い順


def closes_okx(sym, iv):
    d = get("https://www.okx.com/api/v5/market/candles",
            params={"instId": f"{sym}-USDT", "bar": BARS[iv][1], "limit": 181})["data"]
    return [float(x[4]) for x in reversed(d)]  # 新しい順で返るので反転


def closes_hl(name, iv):
    end = int(time.time() * 1000)
    k = post("https://api.hyperliquid.xyz/info", {"type": "candleSnapshot", "req": {
        "coin": name, "interval": iv, "startTime": end - BARS[iv][2] * 1000, "endTime": end}})
    return [float(x["c"]) for x in k][-181:]  # 古い順


def rsi(closes, n=RSI_LEN):
    """ワイルダー方式のRSI（TradingViewなどと同じ計算）。最後の足は確定前の値も含む"""
    if not closes or len(closes) < n + 1:
        return None
    ch = [b - a for a, b in zip(closes, closes[1:])]
    up = sum(max(x, 0) for x in ch[:n]) / n
    dn = sum(max(-x, 0) for x in ch[:n]) / n
    for x in ch[n:]:
        up = (up * (n - 1) + max(x, 0)) / n
        dn = (dn * (n - 1) + max(-x, 0)) / n
    if dn == 0:
        return 100.0
    return 100 - 100 / (1 + up / dn)


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


def fetch_prices(symbols, tradable):
    """4時間足と1時間足の終値。Hyperliquidにある銘柄はそこから、無ければ Binance → OKX"""
    fails = {"Binance": 0, "OKX": 0}

    def closes(s, iv):
        hl = (tradable.get(s) or {}).get("hl")
        c = safe(lambda: closes_hl(hl, iv)) if hl else None
        if c and len(c) >= 42:
            time.sleep(1.0)  # Hyperliquidの呼び出し上限（1分あたり）に余裕を持たせる
            return c
        for name, fn in (("Binance", closes_binance), ("OKX", closes_okx)):
            if fails[name] >= 3:
                continue  # 3回続けて失敗した取引所は以降使わない
            c = safe(lambda: fn(s, iv))
            time.sleep(0.2)
            if c and len(c) >= 42:
                fails[name] = 0
                return c
            fails[name] += 1
        return None

    out = {}
    for s in symbols:
        c4, c1 = closes(s, "4h"), closes(s, "1h")
        if not c4 and not c1:
            continue
        out[s] = {**(trend_stats(c4) if c4 else {}), "rsi_4h": rsi(c4), "rsi_1h": rsi(c1)}
    return out


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
            "vol": float(c.get("dayNtlVlm") or 0), "oi_usd": float(c.get("openInterest") or 0) * mark,
            "max_lev": u.get("maxLeverage")}
    return out


def fetch_ostium():
    d = get("https://metadata-backend.ostium.io/PricePublish/latest-prices")
    return sorted({(x.get("from") or "").upper() for x in d if x.get("from")})


def fetch_tradable():
    """{シンボル: {venues:[...], chg_24h, fr8h, vol, oi_usd}}。値動きはHyperliquidのものを使う"""
    out = {}
    for sym, v in (safe(fetch_hyperliquid, {}) or {}).items():
        out[sym] = {**v, "venues": ["HL"]}
    live = safe(fetch_ostium)
    ost = [x for x in (live or OSTIUM_FALLBACK) if x in OSTIUM_CRYPTO]
    for sym in ost:
        out.setdefault(sym, {"venues": []})["venues"].append("Ostium")
    print(f"  tradable: HL {sum('HL' in v['venues'] for v in out.values())} / Ostium {len(ost)}")
    return out, bool(live)


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


# ---------- 上位銘柄 ----------
def is_tradable_coin(c, meta):
    sym = (c.get("symbol") or "").upper()
    if sym in NOT_TRADABLE_SYM or sym.startswith("USD"):
        return False
    cats = " | ".join(meta.get(c["id"], {}).get("cats", [])).lower()
    return not any(k in cats for k in NOT_TRADABLE_CAT)


def select_top(m, meta, tradable):
    """HyperliquidかOstiumで取引できる銘柄を、時価総額の上から TOP_RANK 個（BTCも含む）"""
    out = []
    for c in sorted(m["top"], key=lambda c: c.get("market_cap_rank") or 999):
        sym = (c.get("symbol") or "").upper()
        if not is_tradable_coin(c, meta) or not (tradable.get(sym) or {}).get("venues"):
            continue
        out.append(c)
        if len(out) >= TOP_RANK:
            break
    return out


def top_symbols(m, meta, tradable):
    """先にトレンド用の価格を取るため、監視する銘柄のシンボルだけ出す"""
    return [c["symbol"].upper() for c in select_top(m, meta, tradable)]


def coin_sectors(c, meta, cats):
    """その銘柄が属するセクター（CoinGeckoのカテゴリ）と、セクター全体の24時間の時価総額の変化"""
    by_name = {x["name"]: x for x in cats if (x.get("market_cap") or 0) >= MIN_CAT_MCAP
               and x.get("market_cap_change_24h") is not None}
    rows = [by_name[n] for n in meta.get(c["id"], {}).get("cats", [])
            if n in by_name and not any(k in n.lower() for k in EXCLUDE_CAT)]
    rows.sort(key=lambda x: -x["market_cap"])
    return [{"name": x["name"], "chg_24h": x["market_cap_change_24h"]} for x in rows[:SECTOR_CHIPS]]


def build_coins(m, meta, tradable, hist, now_ts):
    coins = []
    for c in select_top(m, meta, tradable):
        sym = c["symbol"].upper()
        t = tradable.get(sym) or {"venues": []}
        coins.append({
            "id": c["id"], "sym": sym, "name": c.get("name"), "rank": c.get("market_cap_rank") or 999,
            "price": c.get("current_price"), "mcap": c.get("market_cap"), "vol": c.get("total_volume"),
            "chg_24h": c.get("price_change_percentage_24h_in_currency"),
            "chg_7d": c.get("price_change_percentage_7d_in_currency"),
            "venues": t["venues"], "hl": t.get("hl"), "funding": t.get("fr8h"), "oi": t.get("oi_usd"),
            "oi_24h": pct(t.get("oi_usd"), oi_24h_ago(hist, sym, now_ts)) if t.get("oi_usd") else None,
            "max_lev": t.get("max_lev"), "sectors": coin_sectors(c, meta, m["cats"]),
        })
    return coins


def enrich_coins(coins, prices, chains, hist):
    """銘柄ごとに、価格トレンド・OIのズレ・チェーンの資金の指標・48時間の価格をまとめる"""
    chain_by_sym = {r["symbol"]: r for r in chains if r.get("symbol")}
    for co in coins:
        p = prices.get(co["sym"], {})
        co.update({k: p.get(k) for k in ("vs_ma7", "vs_ma30", "trend", "rsi_1h", "rsi_4h")})
        co["oi_real"] = oi_real(co["oi_24h"], co["chg_24h"])
        co["div"], co["div_text"] = oi_div(co["chg_24h"], co["oi_real"])
        ch = chain_by_sym.get(co["sym"])
        co["chain"] = ({k: ch.get(k) for k in ("name", "score", "label", "label_text", "stable_7d", "dex_7d",
                                                "tvl_7d", "tokens")} if ch else None)
        co["px_series"] = [quiet(lambda s: s["cpx"][co["id"]], s) for s in hist[-47:]] + [co["price"]]


def judge(co):
    """その銘柄自身の指標（トレンド・値動き・FR）だけで状態を決める"""
    c24, c7, tr, m7, fr = co["chg_24h"] or 0, co["chg_7d"], co["trend"], co["vs_ma7"], co["funding"]
    if c24 >= HOT_MOVE or (fr is not None and fr >= FR_HOT):
        return "hot"
    if tr == "up":
        return "dip" if (c24 <= DIP_MOVE or (m7 is not None and m7 < 0)) else "strong"
    if tr is None:  # 4時間足が取れないときは値動きだけで
        if c7 is not None and c7 >= 5 and c24 > 0:
            return "strong"
        if c7 is not None and c7 <= -5 and c24 < 0:
            return "weak"
        return "flat"
    if (m7 or 0) > 0 and c24 >= EARLY_MOVE:
        return "early"
    if tr == "down" and (m7 is None or m7 < 0):
        return "weak"
    return "flat"


def why_text(co):
    tr = {"up": "4時間足は上昇トレンド", "down": "4時間足は下落トレンド", "range": "4時間足はもみ合い"}.get(co["trend"])
    ma = "・".join(x for x in (f"7日線{co['vs_ma7']:+.1f}%" if co["vs_ma7"] is not None else "",
                                f"30日線{co['vs_ma30']:+.1f}%" if co["vs_ma30"] is not None else "") if x)
    head = {"strong": "上昇の流れが続いています。", "early": "7日線を上抜けて、上がり始めています。",
            "dip": "上昇トレンドの中での一時的な下げです。", "flat": "方向がはっきりしません。",
            "weak": "下落の流れが続いています。"}.get(co["status"], "")
    if co["status"] == "hot":
        head = ("FRが高く、ロングが混んでいます。" if (co["funding"] or 0) >= FR_HOT
                else f"24時間で{co['chg_24h']:+.1f}%と急に上がっています。")
    parts = [head]
    if tr:
        parts.append(f"{tr}（{ma}）。" if ma else f"{tr}。")
    if co["div"] and co["div"] != "flat":
        parts.append(f"建玉: {co['div_text']}。")
    return "".join(parts)


def judge_coins(coins):
    for co in coins:
        co["status"] = judge(co)
        co["status_text"] = STATUS[co["status"]][0]
        co["why"] = why_text(co)
    coins.sort(key=lambda c: (STATUS[c["status"]][1], c["rank"]))
    n = 0
    for co in coins:  # 状態の良い順に「条件そろい」を最大 COIN_ENTRY_MAX 個
        ok = (co["status"] in ("strong", "early", "dip") and co["div"] not in ("warn", "weak")
              and (co["funding"] is None or co["funding"] < FR_HOT))
        n += ok
        co["entry"] = bool(ok and n <= COIN_ENTRY_MAX)


def build_flow(coins):
    """値動きと建玉の増減から「今どこに資金が入っているか」をまとめる"""
    for co in coins:  # 24時間の時価総額の増減（ドル）
        c24, mc = co["chg_24h"], co.get("mcap")
        co["mcap_chg"] = mc * c24 / (100 + c24) if (c24 is not None and mc) else None
    by_chg = sorted([c for c in coins if c["chg_24h"] is not None], key=lambda c: -c["chg_24h"])
    ins = [c for c in by_chg if c["chg_24h"] >= FLOW_MOVE]
    outs = [c for c in reversed(by_chg) if c["chg_24h"] <= -FLOW_MOVE]
    new_money = [c["sym"] for c in by_chg if (c.get("oi_real") or 0) >= OI_MOVE and c["chg_24h"] > 0]
    names = lambda xs: "・".join(xs)  # noqa: E731
    up = sum(c["chg_24h"] > 0 for c in by_chg)
    lines = [f"{len(by_chg)}銘柄中 {up}銘柄が24時間で上昇。"]
    if ins:
        lines.append(f"資金が入っているのは {names([c['sym'] for c in ins[:5]])}（+{FLOW_MOVE:g}%以上）。")
    if new_money:
        lines.append(f"{names(new_money[:4])} は建玉も増えていて、新しい買いを伴っています。")
    if outs:
        lines.append(f"抜けているのは {names([c['sym'] for c in outs[:5]])}（−{FLOW_MOVE:g}%以下）。")
    if not ins and not outs:
        lines.append(f"±{FLOW_MOVE:g}%を超えて動いた銘柄はなく、様子見の時間帯です。")
    return {"text": "".join(lines), "in": [c["sym"] for c in ins], "out": [c["sym"] for c in outs],
            "new_money": new_money}


# ---------- サンプルデータ ----------
MOCK_COINS = [  # (id, sym, rank, categories)
    ("ethereum", "ETH", 2, ["Smart Contract Platform", "Layer 1 (L1)", "Ethereum Ecosystem"]),
    ("tether", "USDT", 3, ["Stablecoins"]),
    ("ripple", "XRP", 4, ["Layer 1 (L1)", "XRP Ledger Ecosystem", "Payment Solutions"]),
    ("binancecoin", "BNB", 5, ["Exchange-based Tokens", "BNB Chain Ecosystem", "Layer 1 (L1)"]),
    ("solana", "SOL", 6, ["Solana Ecosystem", "Layer 1 (L1)", "Smart Contract Platform"]),
    ("usd-coin", "USDC", 7, ["Stablecoins"]),
    ("dogecoin", "DOGE", 8, ["Meme", "Proof of Work (PoW)"]),
    ("tron", "TRX", 9, ["Tron Ecosystem", "Layer 1 (L1)"]),
    ("cardano", "ADA", 10, ["Cardano Ecosystem", "Layer 1 (L1)", "Smart Contract Platform"]),
    ("staked-ether", "STETH", 11, ["Liquid Staking Tokens"]),
    ("hyperliquid", "HYPE", 12, ["Hyperliquid Ecosystem", "Decentralized Finance (DeFi)", "Perpetuals"]),
    ("chainlink", "LINK", 13, ["Oracle", "Ethereum Ecosystem", "Real World Assets (RWA)"]),
    ("stellar", "XLM", 14, ["Stellar Ecosystem", "Payment Solutions", "Layer 1 (L1)"]),
    ("sui", "SUI", 15, ["Sui Ecosystem", "Layer 1 (L1)"]),
    ("wrapped-bitcoin", "WBTC", 16, ["Wrapped-Tokens"]),
    ("bitcoin-cash", "BCH", 17, ["Proof of Work (PoW)", "Payment Solutions"]),
    ("avalanche-2", "AVAX", 18, ["Avalanche Ecosystem", "Layer 1 (L1)", "Real World Assets (RWA)"]),
    ("hedera-hashgraph", "HBAR", 19, ["Hedera Ecosystem", "Layer 1 (L1)"]),
    ("litecoin", "LTC", 20, ["Proof of Work (PoW)", "Payment Solutions"]),
    ("the-open-network", "TON", 21, ["TON Ecosystem", "Layer 1 (L1)"]),
    ("shiba-inu", "SHIB", 22, ["Meme", "Ethereum Ecosystem"]),
    ("polkadot", "DOT", 23, ["Polkadot Ecosystem", "Layer 0 (L0)"]),
    ("uniswap", "UNI", 24, ["Decentralized Exchange (DEX)", "Ethereum Ecosystem", "Decentralized Finance (DeFi)"]),
    ("ethena-usde", "USDE", 25, ["Stablecoins"]),
    ("aave", "AAVE", 26, ["Lending/Borrowing", "Decentralized Finance (DeFi)", "Ethereum Ecosystem"]),
    ("pepe", "PEPE", 27, ["Meme", "Ethereum Ecosystem"]),
    ("near", "NEAR", 28, ["Artificial Intelligence (AI)", "Layer 1 (L1)", "Near Protocol Ecosystem"]),
    ("bittensor", "TAO", 29, ["Artificial Intelligence (AI)", "Layer 1 (L1)"]),
    ("ondo-finance", "ONDO", 30, ["Real World Assets (RWA)", "Ethereum Ecosystem"]),
]
MOCK_COIN_CHG = {"ETH": 0.4, "XRP": 1.4, "BNB": 2.0, "SOL": 9.8, "DOGE": 5.4, "TRX": 0.9, "ADA": -0.6,
                 "HYPE": 3.1, "LINK": 1.9, "XLM": 0.3, "SUI": 8.9, "BCH": 0.7, "AVAX": -1.0, "HBAR": -0.2,
                 "LTC": 1.0, "TON": -1.8, "SHIB": 2.4, "DOT": -1.5, "UNI": 2.6, "AAVE": 4.9, "PEPE": 9.2,
                 "NEAR": 5.0, "TAO": 16.0, "ONDO": 3.4}  # BTC比（BTCは+1.2%）
MOCK_FR = {"TAO": 0.0008, "PEPE": 0.0006, "SOL": 0.00015, "DOGE": 0.00025, "AAVE": 0.00022}


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
        hist.append({"ts": ts,
                     "btc_price": 112000 + math.sin(h / 9) * 1500,
                     "cats": {cid: [mc * (1 - h * 0.0008), random.uniform(-1, 3) if cid == "solana-ecosystem"
                                    else random.uniform(-2, 2)] for cid, _, mc in cats_def},
                     "chains": {n: random.randint(20, 80) for n, _ in CHAINS[:10]},
                     "coins": {cid: (1.0 if sym in ("AAVE", "DOGE", "ONDO") and h <= 12 else -0.3)
                               for cid, sym, _, _ in MOCK_COINS},
                     "cpx": {cid: 100 * (1 + MOCK_COIN_CHG.get(sym, 0) / 100 * (48 - min(h, 48)) / 48
                                         + math.sin(h / 4 + rank) * 0.01) for cid, sym, rank, _ in MOCK_COINS}
                            | {"bitcoin": 113400 * (1 - 0.012 * min(h, 24) / 24 + math.sin(h / 5) * 0.004)}})
    top = [{"id": "bitcoin", "symbol": "btc", "name": "Bitcoin", "market_cap_rank": 1, "current_price": 113400,
            "market_cap": 2.25e12, "total_volume": 4.1e10,
            "price_change_percentage_24h_in_currency": 1.2, "price_change_percentage_7d_in_currency": -0.8}]
    for cid, sym, rank, _ in MOCK_COINS:
        c24 = MOCK_COIN_CHG.get(sym, -1.2) + 1.2
        top.append({"id": cid, "symbol": sym.lower(), "name": sym.title(), "market_cap_rank": rank,
                    "current_price": 100 * (1 + MOCK_COIN_CHG.get(sym, 0) / 100), "market_cap": 4e11 / rank,
                    "total_volume": 2e10 / rank,
                    "price_change_percentage_24h_in_currency": c24,
                    "price_change_percentage_7d_in_currency": c24 * 1.6 + random.uniform(-3, 3)})
    meta = {cid: {"cats": cats, "ts": now_ts} for cid, _, _, cats in MOCK_COINS}
    market = {"btc_price": 113400, "btc_24h": 1.2, "btc_7d": -0.8,
              "top": top, "cats": [
        {"id": cid, "name": nm, "market_cap": mc, "market_cap_change_24h": random.uniform(-4, 9),
         "top_3_coins_id": ["coin-a", "coin-b", "coin-c"]} for cid, nm, mc in cats_def]}
    chains = [{"name": n, "symbol": s, "tvl": random.uniform(5e8, 6e10), "tvl_7d": random.uniform(-8, 12),
               "stable": random.uniform(3e8, 8e10), "stable_7d": random.uniform(-5, 10),
               "dex_24h": random.uniform(5e7, 3e9), "dex_7d": random.uniform(-30, 60)} for n, s in CHAINS[:12]]
    derivs = {s: {"funding": random.choice([0.00005, 0.0001, 0.00015, 0.0003, 0.0007]),
                  "oi": random.uniform(1e8, 2e10), "oi_24h": random.uniform(-10, 30)}
              for _, s in CHAINS if s}
    prices = {}
    for s in dict.fromkeys([x for _, x in CHAINS if x] + [x for _, x, _, _ in MOCK_COINS]):
        drift = random.uniform(-0.004, 0.005)
        c, v = [], 100.0
        for _ in range(181):
            v *= 1 + drift + random.uniform(-0.02, 0.02)
            c.append(v)
        prices[s] = {**trend_stats(c), "rsi_4h": rsi(c), "rsi_1h": rsi(c[-40:])}
    syms = ["BTC", "ETH", "SOL", "XRP", "DOGE", "WIF", "BONK", "PEPE", "POPCAT", "FARTCOIN", "JUP", "RAY", "PYTH",
            "JTO", "RENDER", "TAO", "FET", "VIRTUAL", "AI16Z", "ONDO", "PENDLE", "AAVE", "UNI", "MORPHO", "AERO",
            "ZRO", "STRK", "ZK", "OP", "ARB", "CETUS", "DEEP", "NAVX", "HYPE", "PURR", "LINK", "ENA", "ETHFI"]
    tradable = {x: {"venues": ["HL"] + (["Ostium"] if x in ("BTC", "ETH", "SOL", "XRP", "LINK") else []),
                    "chg_24h": random.uniform(-9, 14), "fr8h": random.choice([-0.0001, 0.00005, 0.0001, 0.0003, 0.0008]),
                    "vol": random.uniform(2e6, 8e8)} for x in syms}
    for _, sym, _, _ in [("bitcoin", "BTC", 1, [])] + MOCK_COINS:
        if sym in ("USDT", "USDC", "STETH", "WBTC", "USDE"):
            continue
        t = tradable.setdefault(sym, {"venues": ["HL"], "chg_24h": MOCK_COIN_CHG.get(sym, 0) + 1.2,
                                      "vol": random.uniform(2e6, 8e8)})
        t.update({"hl": ("k" + sym) if sym in ("PEPE", "SHIB") else sym, "oi_usd": random.uniform(5e7, 2e9),
                  "fr8h": MOCK_FR.get(sym, random.choice([0.00004, 0.0001, 0.00012])),
                  "max_lev": 25 if sym in ("ETH", "SOL", "XRP") else 10})
    kinds = ["Dexs", "Lending", "Derivatives", "Yield", "エコシステム"]
    tokens = {}
    for c in chains:
        tokens[c["name"]] = sorted([coin_row(x, tradable[x], name=None, cat=random.choice(kinds),
                                             tvl_7d=random.uniform(-10, 20), px_7d=random.uniform(-20, 40))
                                    for x in random.sample(syms[2:], 5)], key=lambda r: -r["vol"])
    for snap in hist[-26:]:  # 建玉の24時間変化を出すため、Hyperliquidの建玉の履歴も作る
        snap["oi"] = {k: v["oi_usd"] / random.Random(k).uniform(0.85, 1.25) for k, v in tradable.items() if v.get("oi_usd")}
    return hist, market, chains, ("Mock", derivs), prices, tokens, tradable, meta


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
        hist, market, chains, (dsrc, derivs), prices, tokens, tradable, meta = mock_all(now_ts)
        cache = MockCache(tradable)
    else:
        hist = load(HIST_PATH, [])
        meta = load(META_PATH, {})
        if not CG_KEY:
            warnings.append("COINGECKO_API_KEY が未設定です。GitHubのSecretsに登録してください。")
        print("CoinGecko...")
        market = fetch_market()
        meta = refresh_meta(market["top"], meta, now_ts)
        print("DefiLlama...")
        chains = safe(fetch_chains, [])
        if not chains:
            warnings.append("DefiLlamaからチェーンのデータを取れませんでした。")
        print("取引できる銘柄...")
        tradable, ostium_live = fetch_tradable()
        if not any("HL" in v["venues"] for v in tradable.values()):
            warnings.append("Hyperliquidの銘柄一覧を取れませんでした。取引できる銘柄の表示が少なくなっています。")
        if not ostium_live:
            warnings.append("Ostiumの銘柄一覧を取れなかったので、主要銘柄の固定リストで代用しています。")
        missing = [c["id"] for c in select_top(market, meta, tradable) if c["id"] not in meta]
        if missing:
            warnings.append(f"{len(missing)}銘柄のセクター情報がまだ取れていません（数時間で埋まります）。")
        print("先物...")
        syms = [r["symbol"] for r in chains if r["symbol"]]
        derivs = derivs_from_hl(tradable, syms, hist, now_ts)
        dsrc = "Hyperliquid" if derivs else None
        rest = [s for s in syms if s not in derivs]
        if rest:  # Hyperliquidに無い銘柄だけ Binance → OKX
            src2, d2 = fetch_derivs(rest)
            derivs.update(d2)
            if d2:
                dsrc = f"{dsrc} + {src2}" if dsrc else src2
        if not derivs:
            warnings.append("先物データを取れませんでした。FRとOIなしで計算しています。")
        print("価格...")
        prices = fetch_prices(top_symbols(market, meta, tradable), tradable)
        if not prices:
            warnings.append("価格データを取れませんでした。トレンドとOIのズレは表示されません。")
        cache = CatCache(CAT_PATH, now_ts, CAT_BUDGET)

    for r in chains:
        d = derivs.get(r["symbol"] or "", {})
        r.update({"funding": d.get("funding"), "oi": d.get("oi"), "oi_24h": d.get("oi_24h")})
        p = prices.get(r["symbol"] or "", {})
        r.update({k: p.get(k) for k in ("price", "px_24h", "vs_ma7", "vs_ma30", "trend")})
    score_chains(chains)

    print("上位銘柄...")
    coins = build_coins(market, meta, tradable, hist, now_ts)
    shown = [r for r in chains if r["symbol"] in {c["sym"] for c in coins}]  # 上位銘柄がそのチェーンの通貨のものだけ
    if not MOCK:
        print("取引できるトークン...")
        tokens = safe(lambda: fetch_tokens([r["name"] for r in shown],
                                           {r["name"]: r["symbol"] for r in chains if r["symbol"]},
                                           tradable, cache, market["cats"]), {}) or {}
    for r in chains:
        r["tokens"] = tokens.get(r["name"], [])
    cache.save()
    enrich_coins(coins, prices, chains, hist)
    judge_coins(coins)
    flow = build_flow(coins)

    snap = {"ts": now_ts, "btc_price": market["btc_price"],
            "chains": {r["name"]: r["score"] for r in chains},
            "cpx": {c["id"]: c["price"] for c in coins if c["price"]},
            # Hyperliquidの建玉（24時間前との比較用。古いものは消す）
            "oi": {k: round(v["oi_usd"]) for k, v in tradable.items()
                   if v.get("oi_usd") and (k in derivs or any(c["sym"] == k for c in coins))}}
    hist = (hist + [snap])[-HIST_MAX:]
    for s in hist:
        if s["ts"] < now_ts - 26 * 3600:
            s.pop("oi", None)
    for c in coins:
        c.pop("oi", None)

    data = {
        "updated": datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
        "mock": MOCK, "top_rank": TOP_RANK,
        "flow": flow,
        "coins": coins,
        "deriv_source": dsrc,
        "warnings": warnings,
    }
    DOCS.mkdir(exist_ok=True)
    DATA_PATH.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    if not MOCK:
        HIST_PATH.write_text(json.dumps(hist, separators=(",", ":")))
        META_PATH.parent.mkdir(exist_ok=True)
        META_PATH.write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")))
    entry = [c["sym"] for c in coins if c["entry"]]
    print(f"done: {DATA_PATH.name} / coins={len(coins)} entry={entry} deriv={dsrc}")


if __name__ == "__main__":
    main()
