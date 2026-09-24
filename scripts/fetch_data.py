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
    btc = cg("/simple/price", {"ids": "bitcoin", "vs_currencies": "usd",
                               "include_24hr_change": "true"})["bitcoin"]
    cats = cg("/coins/categories")
    return {"btc_dom": g["market_cap_percentage"]["btc"], "btc_price": btc["usd"],
            "btc_24h": btc.get("usd_24h_change") or 0.0, "cats": cats}


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
    rows.sort(key=lambda r: -1 if r["score"] is None else r["score"], reverse=True)


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
    market = {"btc_dom": 57.1, "btc_price": 113400, "btc_24h": 1.2, "cats": [
        {"id": cid, "name": nm, "market_cap": mc, "market_cap_change_24h": random.uniform(-4, 9),
         "top_3_coins_id": ["coin-a", "coin-b", "coin-c"]} for cid, nm, mc in cats_def]}
    chains = [{"name": n, "symbol": s, "tvl": random.uniform(5e8, 6e10), "tvl_7d": random.uniform(-8, 12),
               "stable": random.uniform(3e8, 8e10), "stable_7d": random.uniform(-5, 10),
               "dex_24h": random.uniform(5e7, 3e9), "dex_7d": random.uniform(-30, 60)} for n, s in CHAINS[:12]]
    derivs = {s: {"funding": random.choice([0.00005, 0.0001, 0.00015, 0.0003, 0.0007]),
                  "oi": random.uniform(1e8, 2e10), "oi_24h": random.uniform(-10, 30)}
              for _, s in CHAINS if s}
    return hist, market, chains, ("Mock", derivs)


# ---------- メイン ----------
def main():
    now_ts = int(time.time())
    warnings = []
    if MOCK:
        hist, market, chains, (dsrc, derivs) = mock_all(now_ts)
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

    for r in chains:
        d = derivs.get(r["symbol"] or "", {})
        r.update({"funding": d.get("funding"), "oi": d.get("oi"), "oi_24h": d.get("oi_24h")})
    score_chains(chains)

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
                "phase": phase_text(dom7_delta)},
        "dom_series": [[s["ts"], round(s["btc_dom"], 3)] for s in hist[-24 * 7:]],
        "chains": chains,
        "sectors": sectors,
        "deriv_source": dsrc,
        "warnings": warnings,
    }
    DOCS.mkdir(exist_ok=True)
    DATA_PATH.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    if not MOCK:
        HIST_PATH.write_text(json.dumps(hist, separators=(",", ":")))
    print(f"done: {DATA_PATH.name} / chains={len(chains)} sectors={len(sectors)} deriv={dsrc}")


if __name__ == "__main__":
    main()
