#!/usr/bin/env python3
"""週末バックテスト（新高値ブレイク）

ルール
  買い: 終値が過去250営業日（52週）の終値の最高値を上回った日の翌営業日の寄付で買う
  売り: 終値が直近20営業日の安値（当日を含まない）を下回った日の翌営業日の寄付で売る
  同じ銘柄は保有中に重ねて買わない

比較する5パターン（買う日の条件）
  all    : 絞り込みなし
  trend  : 上昇トレンド（終値 > 25日線 > 75日線 > 200日線）
  volume : 出来高が直近20日平均（当日を含まない）の1.5倍以上
  value  : 売買代金（終値×出来高）1億円以上
  combo  : 上の3条件すべて

出力
  weekly/<実行日>/index.html  週次レポート
  weekly/<実行日>/summary.json
  weekly/index.html           週次レポートの一覧

使い方
  python tools/weekly_backtest.py              # 実データ（yfinance）
  python tools/weekly_backtest.py --synthetic  # 動作確認用の合成データ
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import io
import json
import math
import os
import pickle
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
JST = dt.timezone(dt.timedelta(hours=9))

HIGH_WIN = 250      # 52週 ≒ 250営業日
LOW_WIN = 20        # 直近20日安値
VOL_WIN = 20
VOL_MULT = 1.5
VALUE_MIN = 1e8     # 売買代金 1億円
HIST_YEARS = 3      # 過去検証の期間
TRADE_YEN = 1_000_000  # 1回あたりの購入金額（累計損益の表示用）

VARIANTS = [
    ("all", "絞り込みなし"),
    ("trend", "上昇トレンド"),
    ("volume", "出来高1.5倍"),
    ("value", "売買代金1億円"),
    ("combo", "3条件すべて"),
]
VNAME = dict(VARIANTS)

JPX_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
JPX_URLS = [
    "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx",
    "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls",
]


# ---------------------------------------------------------------- 銘柄一覧
def load_universe() -> pd.DataFrame:
    """東証の内国株式（プライム・スタンダード・グロース）。取れなければ daily/ の株探データから。"""
    try:
        import requests
        ua = {"User-Agent": "Mozilla/5.0"}
        urls = list(JPX_URLS)
        try:  # 一覧ページのリンクを優先（ファイル名が変わっても追従する）
            pg = requests.get(JPX_PAGE, timeout=60, headers=ua).text
            for h in re.findall(r'href="([^"]+data_j\.xlsx?)"', pg):
                urls.insert(0, h if h.startswith("http") else "https://www.jpx.co.jp" + h)
        except Exception:  # noqa: BLE001
            pass
        r = None
        for u in urls:
            r = requests.get(u, timeout=60, headers=ua)
            if r.ok:
                break
        r.raise_for_status()
        df = pd.read_excel(io.BytesIO(r.content), dtype=str)
        df.columns = [str(c).strip() for c in df.columns]
        mkt_col = next(c for c in df.columns if "市場" in c)
        df = df[df[mkt_col].str.contains("内国株式", na=False)]
        out = pd.DataFrame({
            "code": df["コード"].str.strip(),
            "name": df["銘柄名"].str.strip(),
            "market": df[mkt_col].str.replace("（内国株式）", "", regex=False).str.strip(),
            "sector": df.get("33業種区分", pd.Series("", index=df.index)).fillna("").str.strip(),
        })
        print(f"[universe] JPX一覧から {len(out)} 銘柄", flush=True)
        return out.reset_index(drop=True)
    except Exception as e:  # noqa: BLE001
        print(f"[universe] JPX一覧を取れず ({e})。daily/ の株探データで代用", flush=True)
    rows = {}
    for p in sorted((ROOT / "daily").glob("*" + "/data.txt")):
        sec = None
        for line in p.read_text(encoding="utf-8").splitlines():
            if line in ("SEC", "UP", "DOWN", "HIGH"):
                sec = line
                continue
            if sec in ("UP", "DOWN", "HIGH"):
                c = line.split("|")
                if len(c) >= 3 and re.fullmatch(r"[0-9A-Z]{4}", c[0]):
                    rows[c[0]] = (c[1], c[2])
    out = pd.DataFrame([{"code": k, "name": v[0], "market": v[1], "sector": ""} for k, v in rows.items()])
    print(f"[universe] daily/ から {len(out)} 銘柄", flush=True)
    return out


# ---------------------------------------------------------------- 株価
def download_prices(codes: list[str], start: dt.date, cache: Path | None) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    prices: dict[str, pd.DataFrame] = {}
    if cache and cache.exists():
        try:
            prices = pickle.loads(cache.read_bytes())
            print(f"[prices] キャッシュから {len(prices)} 銘柄", flush=True)
        except Exception:  # noqa: BLE001
            prices = {}
    todo = [c for c in codes if c not in prices]
    chunk = 100
    for i in range(0, len(todo), chunk):
        part = todo[i:i + chunk]
        tickers = [f"{c}.T" for c in part]
        for attempt in range(4):
            try:
                df = yf.download(tickers, start=start.isoformat(), auto_adjust=True, group_by="ticker",
                                 threads=True, progress=False)
                break
            except Exception as e:  # noqa: BLE001
                wait = 30 * (attempt + 1)
                print(f"[prices] 取得失敗 {e} / {wait}秒待って再試行", flush=True)
                time.sleep(wait)
        else:
            continue
        for c, t in zip(part, tickers):
            try:
                d = df[t] if isinstance(df.columns, pd.MultiIndex) else df
                d = d[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Open", "Close"])
                d = d[(d["Close"] > 0) & (d["Open"] > 0)]
                if len(d) > 0:
                    d.index = pd.to_datetime(d.index).tz_localize(None).normalize()
                    prices[c] = d.astype(float)
            except Exception:  # noqa: BLE001
                pass
        print(f"[prices] {min(i + chunk, len(todo))}/{len(todo)} 取得済み（有効 {len(prices)}）", flush=True)
        time.sleep(1.5)
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(pickle.dumps(prices))
    return prices


def synthetic_prices(n: int = 400, days: int = 1100, end: dt.date | None = None, seed: int = 7):
    rng = np.random.default_rng(seed)
    end = end or dt.date.today()
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=days)
    uni, prices = [], {}
    for k in range(n):
        code = f"{1300 + k * 7:04d}"
        drift = rng.normal(0.0004, 0.0008)
        vol = rng.uniform(0.012, 0.035)
        r = rng.normal(drift, vol, days)
        close = 1000 * np.exp(np.cumsum(r)) * rng.uniform(0.2, 5)
        opn = close * np.exp(rng.normal(0, vol / 3, days))
        hi = np.maximum(opn, close) * np.exp(np.abs(rng.normal(0, vol / 2, days)))
        lo = np.minimum(opn, close) * np.exp(-np.abs(rng.normal(0, vol / 2, days)))
        v = rng.lognormal(11, 1.0, days) * (1 + 2 * (r > 2 * vol))
        start_i = int(rng.integers(0, 300)) if k % 10 == 0 else 0  # 一部は新規上場扱い
        d = pd.DataFrame({"Open": opn, "High": hi, "Low": lo, "Close": close, "Volume": v}, index=idx).iloc[start_i:]
        prices[code] = d
        uni.append({"code": code, "name": f"テスト{k}", "market": ["プライム", "スタンダード", "グロース"][k % 3], "sector": ""})
    return pd.DataFrame(uni), prices


def clean_prices(d: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    """1日で半値以下・2倍超の動きは値幅制限上ありえないので、分割の未調整などデータの誤りとみなし、
    その日より前のデータを捨てる。戻り値の2つ目は切り捨てたかどうか"""
    pc = d["Close"].shift(1)
    bad = (d["Close"] / pc < 0.55) | (d["Close"] / pc > 2.0) | (d["Open"] / pc < 0.55) | (d["Open"] / pc > 2.0)
    if bad.any():
        last_bad = bad[bad].index[-1]
        return d[d.index >= last_bad], True
    return d, False


# ---------------------------------------------------------------- シグナルと売買
def add_indicators(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    c = d["Close"]
    d["prev_high"] = c.shift(1).rolling(HIGH_WIN, min_periods=HIGH_WIN).max()
    d["stop"] = d["Low"].shift(1).rolling(LOW_WIN, min_periods=LOW_WIN).min()
    d["ma25"] = c.rolling(25).mean()
    d["ma75"] = c.rolling(75).mean()
    d["ma200"] = c.rolling(200).mean()
    d["vavg"] = d["Volume"].shift(1).rolling(VOL_WIN, min_periods=VOL_WIN).mean()
    d["vratio"] = d["Volume"] / d["vavg"]
    d["value"] = c * d["Volume"]
    d["sig"] = c > d["prev_high"]
    d["f_trend"] = (c > d["ma25"]) & (d["ma25"] > d["ma75"]) & (d["ma75"] > d["ma200"])
    d["f_volume"] = d["vratio"] >= VOL_MULT
    d["f_value"] = d["value"] >= VALUE_MIN
    d["exit_sig"] = c < d["stop"]
    return d


def variant_mask(d: pd.DataFrame, v: str) -> np.ndarray:
    s = d["sig"].to_numpy(dtype=bool)
    if v == "all":
        return s
    if v == "combo":
        return s & d["f_trend"].to_numpy(bool) & d["f_volume"].to_numpy(bool) & d["f_value"].to_numpy(bool)
    return s & d[f"f_{v}"].to_numpy(bool)


def simulate(d: pd.DataFrame, mask: np.ndarray, from_ts: pd.Timestamp) -> list[dict]:
    """シグナル日 >= from_ts の売買を1銘柄分シミュレーション"""
    idx = d.index
    opn = d["Open"].to_numpy()
    close = d["Close"].to_numpy()
    exit_sig = d["exit_sig"].to_numpy(bool)
    stop = d["stop"].to_numpy()
    n = len(d)
    trades = []
    i = int(np.searchsorted(idx.values, from_ts.to_datetime64()))
    while i < n:
        if not mask[i]:
            i += 1
            continue
        sig_i = i
        if sig_i + 1 >= n:  # 金曜のシグナル → 来週の寄付で買い予定
            trades.append({"signal": idx[sig_i], "entry": None, "entry_px": None, "exit": None, "exit_px": None,
                           "status": "pending", "last_px": close[sig_i], "stop_now": stop[sig_i] if not math.isnan(stop[sig_i]) else None})
            break
        e = sig_i + 1
        entry_px = opn[e]
        j = e
        exit_i = None
        while j < n:
            if exit_sig[j]:
                exit_i = j
                break
            j += 1
        if exit_i is not None and exit_i + 1 < n:
            x = exit_i + 1
            trades.append({"signal": idx[sig_i], "entry": idx[e], "entry_px": entry_px, "exit": idx[x],
                           "exit_px": opn[x], "status": "closed", "days": x - e,
                           "ret": opn[x] / entry_px - 1, "exit_trigger": idx[exit_i]})
            i = x  # 売った日以降のシグナルで再び買える
            continue
        last = n - 1
        st = "exit_next" if exit_i is not None else "open"  # exit_next: 金曜に売りシグナル → 来週寄付で売り予定
        s_now = stop[last]
        trades.append({"signal": idx[sig_i], "entry": idx[e], "entry_px": entry_px, "exit": None, "exit_px": None,
                       "status": st, "days": last - e, "ret": close[last] / entry_px - 1, "last_px": close[last],
                       "stop_now": None if math.isnan(s_now) else s_now})
        break
    return trades


# ---------------------------------------------------------------- 集計
def stats(tr: pd.DataFrame) -> dict:
    c = tr[tr["status"] == "closed"]
    if c.empty:
        return {"n": 0}
    r = c["ret"]
    w, l = r[r > 0], r[r <= 0]
    return {
        "n": int(len(c)),
        "win": float((r > 0).mean()),
        "avg": float(r.mean()),
        "med": float(r.median()),
        "avg_win": float(w.mean()) if len(w) else 0.0,
        "avg_loss": float(l.mean()) if len(l) else 0.0,
        "pf": float(w.sum() / -l.sum()) if l.sum() < 0 else float("inf"),
        "days": float(c["days"].mean()),
        "best": float(r.max()),
        "worst": float(r.min()),
        "pnl": float(r.sum() * TRADE_YEN),
        "open": int((tr["status"].isin(["open", "exit_next"])).sum()),
    }


def max_concurrent(tr: pd.DataFrame, last_day: pd.Timestamp) -> int:
    t = tr[tr["entry"].notna()]
    if t.empty:
        return 0
    ev = []
    for _, r in t.iterrows():
        ev.append((r["entry"], 1))
        ev.append((r["exit"] if pd.notna(r["exit"]) else last_day + pd.Timedelta(days=1), -1))
    ev.sort(key=lambda x: (x[0], x[1]))
    cur = best = 0
    for _, k in ev:
        cur += k
        best = max(best, cur)
    return best


# ---------------------------------------------------------------- HTML
def fmt_pct(x, sign=True, digits=1):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    return f"{x * 100:+.{digits}f}%" if sign else f"{x * 100:.{digits}f}%"


def fmt_num(x, digits=0):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:,.{digits}f}"


def fmt_oku(x):
    return "—" if x is None or math.isnan(x) else f"{x / 1e8:,.1f}億"


def md(ts):
    return "—" if ts is None or pd.isna(ts) else f"{ts.month}/{ts.day}"


def esc(s):
    return html.escape(str(s))


def cls_ret(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return ""
    return "up" if x > 0 else ("down" if x < 0 else "")


CSS = """
:root{--bg:#f3f4f7;--panel:#fff;--ink:#1b2130;--muted:#5d6577;--line:#dfe2ea;--accent:#1f3a8a;--accent-soft:#e7ecfb;
--up:#c8283a;--up-soft:#fbe7ea;--down:#1f62c4;--down-soft:#e5eefb;--warn:#b26a00;--warn-soft:#fff2dc;--ok:#1d7a4f;--ok-soft:#e3f4ea}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#10131b;--panel:#181c27;--ink:#e7e9f0;--muted:#9aa2b5;--line:#2a3040;
--accent:#8fa8ff;--accent-soft:#1e2742;--up:#ff6b7a;--up-soft:#3a1d24;--down:#6aa6ff;--down-soft:#172741;--warn:#ffb648;--warn-soft:#3a2b12;--ok:#5fd39b;--ok-soft:#14301f;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#10131b;--panel:#181c27;--ink:#e7e9f0;--muted:#9aa2b5;--line:#2a3040;
--accent:#8fa8ff;--accent-soft:#1e2742;--up:#ff6b7a;--up-soft:#3a1d24;--down:#6aa6ff;--down-soft:#172741;--warn:#ffb648;--warn-soft:#3a2b12;--ok:#5fd39b;--ok-soft:#14301f;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"Hiragino Sans","Yu Gothic",sans-serif;line-height:1.7;font-size:15px}
main{max-width:980px;margin:0 auto;padding:28px 16px 60px}
a{color:var(--accent)}
h1{font-size:1.5rem;margin:0 0 2px}
h2{font-size:1.15rem;margin:34px 0 10px;padding-left:10px;border-left:4px solid var(--accent)}
h3{font-size:1rem;margin:20px 0 8px}
.sub{color:var(--muted);margin:0 0 18px;font-size:.9rem}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.concl p{margin:0 0 10px}.concl p:last-child{margin:0}
.rule{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.rule div{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.rule b{display:block;font-size:.8rem;color:var(--muted);font-weight:600}
.tw{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:.88rem}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{background:var(--accent-soft);color:var(--ink);font-weight:700;position:sticky;top:0}
td.l,th.l{text-align:left}
tr:last-child td{border-bottom:0}
td{font-variant-numeric:tabular-nums}
.up{color:var(--up)}.down{color:var(--down)}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:.78rem;font-weight:700}
.p-hold{background:var(--ok-soft);color:var(--ok)}.p-exit{background:var(--warn-soft);color:var(--warn)}
.p-pend{background:var(--accent-soft);color:var(--accent)}.p-closed{background:var(--line);color:var(--muted)}
.ok{color:var(--ok);font-weight:700}.ng{color:var(--muted)}
.hl td{background:var(--accent-soft)}
details{margin-top:10px}summary{cursor:pointer;color:var(--accent);font-weight:600}
.note{color:var(--muted);font-size:.85rem}
.chart{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px}
.chart svg{width:100%;height:auto;display:block}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:.82rem;color:var(--muted);margin-top:6px}
.legend i{display:inline-block;width:14px;height:3px;vertical-align:middle;margin-right:5px;border-radius:2px}
"""

COLORS = {"all": "#8a93a6", "trend": "#1d7a4f", "volume": "#b26a00", "value": "#1f62c4", "combo": "#c8283a"}


def status_pill(st):
    return {"open": '<span class="pill p-hold">保有中</span>',
            "exit_next": '<span class="pill p-exit">月曜寄付で売り</span>',
            "pending": '<span class="pill p-pend">月曜寄付で買い</span>',
            "closed": '<span class="pill p-closed">決済済み</span>'}[st]


def cum_chart(all_trades: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> str:
    W, H, L, R, T, B = 900, 300, 64, 16, 14, 30
    series = {}
    lo, hi = 0.0, 0.0
    for v, _ in VARIANTS:
        c = all_trades[(all_trades["variant"] == v) & (all_trades["status"] == "closed")].sort_values("exit")
        if c.empty:
            continue
        y = (c["ret"] * TRADE_YEN / 1e4).cumsum().to_numpy()  # 万円
        series[v] = (c["exit"].to_numpy(), y)
        lo, hi = min(lo, y.min()), max(hi, y.max())
    if not series:
        return '<p class="note">決済済みのトレードがまだありません。</p>'
    span = (end - start).days or 1
    pad = (hi - lo) * 0.06 or 1
    lo, hi = lo - pad, hi + pad

    def X(t):
        return L + (pd.Timestamp(t) - start).days / span * (W - L - R)

    def Y(v):
        return T + (hi - v) / (hi - lo) * (H - T - B)

    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="累計損益の推移">']
    step = 10 ** math.floor(math.log10(max(hi - lo, 1)))
    for m in (1, 2, 5, 10):
        if (hi - lo) / (step * m) <= 6:
            step *= m
            break
    g = math.ceil(lo / step) * step
    while g <= hi:
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{Y(g):.1f}" y2="{Y(g):.1f}" stroke="var(--line)" stroke-width="{1.6 if g == 0 else 1}"/>')
        out.append(f'<text x="{L - 6}" y="{Y(g) + 4:.1f}" text-anchor="end" font-size="11" fill="var(--muted)">{g:,.0f}万</text>')
        g += step
    for yr in range(start.year, end.year + 1):
        for mo in (1, 7):
            t = pd.Timestamp(yr, mo, 1)
            if start <= t <= end:
                out.append(f'<text x="{X(t):.1f}" y="{H - 8}" text-anchor="middle" font-size="11" fill="var(--muted)">{yr}/{mo}</text>')
    for v, (xs, ys) in series.items():
        pts = [f"{X(start):.1f},{Y(0):.1f}"] + [f"{X(a):.1f},{Y(b):.1f}" for a, b in zip(xs, ys)]
        w = 2.4 if v == "combo" else 1.6
        out.append(f'<polyline fill="none" stroke="{COLORS[v]}" stroke-width="{w}" points="{" ".join(pts)}"/>')
    out.append("</svg>")
    leg = "".join(f'<span><i style="background:{COLORS[v]}"></i>{esc(VNAME[v])}</span>' for v in series)
    return "\n".join(out) + f'<div class="legend">{leg}</div>'


def build_report(run_date: dt.date, uni: pd.DataFrame, data: dict[str, pd.DataFrame], synthetic: bool) -> tuple[str, dict]:
    info = uni.set_index("code").to_dict("index")
    last_day = max(d.index[-1] for d in data.values())
    week_start = last_day - pd.Timedelta(days=last_day.weekday())  # その週の月曜
    hist_start = last_day - pd.DateOffset(years=HIST_YEARS)
    # 今週と、最近の保有追跡のため、シグナルは過去検証の開始日から全部シミュレーション
    rows = []
    week_sigs = []
    n_enough = 0
    n_cut = 0
    for code, raw in data.items():
        raw, cut = clean_prices(raw)
        n_cut += cut
        if len(raw) < HIGH_WIN + 5:
            continue
        n_enough += 1
        d = add_indicators(raw)
        for v, _ in VARIANTS:
            for t in simulate(d, variant_mask(d, v), hist_start):
                t.update(code=code, variant=v)
                rows.append(t)
        w = d[(d.index >= week_start) & d["sig"]]
        for ts, r in w.iterrows():
            pos = d.index.get_loc(ts)
            nxt = d.iloc[pos + 1] if pos + 1 < len(d) else None
            week_sigs.append({
                "code": code, "date": ts, "close": r["Close"], "prev_high": r["prev_high"],
                "vratio": r["vratio"], "value": r["value"],
                "trend": bool(r["f_trend"]), "volume": bool(r["f_volume"]), "value_ok": bool(r["f_value"]),
                "entry_px": None if nxt is None else nxt["Open"],
                "last": d["Close"].iloc[-1], "stop_now": d["stop"].iloc[-1],
            })
    tr = pd.DataFrame(rows)
    if tr.empty:
        tr = pd.DataFrame(columns=["code", "variant", "signal", "entry", "entry_px", "exit", "exit_px", "status", "days", "ret"])
    for c in ("signal", "entry", "exit"):
        tr[c] = pd.to_datetime(tr[c])

    st = {v: stats(tr[tr["variant"] == v]) for v, _ in VARIANTS}
    for v, _ in VARIANTS:
        st[v]["maxpos"] = max_concurrent(tr[tr["variant"] == v], last_day)

    # 年別（決済日ベースではなく買い日ベース）
    yearly = {}
    closed = tr[tr["status"] == "closed"].copy()
    if not closed.empty:
        closed["year"] = closed["entry"].dt.year
        for (v, y), g in closed.groupby(["variant", "year"]):
            yearly.setdefault(v, {})[int(y)] = (len(g), float((g["ret"] > 0).mean()), float(g["ret"].mean()), float(g["ret"].sum() * TRADE_YEN))

    # 今週のシグナル
    ws = pd.DataFrame(week_sigs)
    if not ws.empty:
        ws["combo"] = ws["trend"] & ws["volume"] & ws["value_ok"]
        ws = ws.sort_values(["combo", "date", "value"], ascending=[False, True, False])
        # 1銘柄は今週の最初のシグナル日だけ残す（同じ週に何度も更新するため）
        ws = ws.drop_duplicates("code", keep="first")

    # 今週決済・現在保有（3条件すべて）
    cb = tr[tr["variant"] == "combo"]
    week_closed = cb[(cb["status"] == "closed") & (cb["exit"] >= week_start)].sort_values("exit")
    holding = cb[cb["status"].isin(["open", "exit_next", "pending"])].sort_values("signal", ascending=False)

    # 今週エントリーしたものの今週の成績（各パターン）
    wk = {}
    for v, _ in VARIANTS:
        t = tr[(tr["variant"] == v) & (tr["entry"] >= week_start)]
        rr = t["ret"].dropna()
        wk[v] = (len(t), 0, float(rr.mean()) if len(rr) else float("nan"), float((rr > 0).mean()) if len(rr) else float("nan"))

    # ---------------- 結論の文章
    n_ws = 0 if ws.empty else len(ws)
    n_combo = 0 if ws.empty else int(ws["combo"].sum())
    best_v = max((v for v, _ in VARIANTS if st[v]["n"] >= 20), key=lambda v: st[v]["avg"], default=None)
    s_all, s_cb = st["all"], st["combo"]
    concl = []
    concl.append(f"今週（{md(week_start)}〜{md(last_day)}）に終値で52週高値を更新したのは <b>{n_ws}銘柄</b>。"
                 f"そのうち3条件（上昇トレンド・出来高1.5倍・売買代金1億円）をすべて満たしたのは <b>{n_combo}銘柄</b>でした。")
    if s_all.get("n") and s_cb.get("n"):
        concl.append(
            f"過去{HIST_YEARS}年でこのルールを機械的に続けると、絞り込みなしは {s_all['n']:,}回の売買で勝率{fmt_pct(s_all['win'], False, 0)}・"
            f"1回平均{fmt_pct(s_all['avg'])}、3条件すべては {s_cb['n']:,}回で勝率{fmt_pct(s_cb['win'], False, 0)}・1回平均{fmt_pct(s_cb['avg'])}。"
            + (f" 1回平均の損益が最も良いのは「{VNAME[best_v]}」でした。" if best_v else ""))
        concl.append(
            f"勝率は低めでも、勝ちの平均（3条件すべてで{fmt_pct(s_cb['avg_win'])}）が負けの平均（{fmt_pct(s_cb['avg_loss'])}）より大きいかどうかが、"
            f"このルールの成否の分かれ目です。3条件すべての損益比（PF）は {s_cb['pf']:.2f}（1を超えればプラス）。")
    concl.append(f"3条件すべてで現在保有中（月曜の売買予定を含む）は {len(holding)}銘柄、今週決済したのは {len(week_closed)}銘柄です。")

    # ---------------- HTML 本体
    H = []
    H.append(f'<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
             f'<meta name="robots" content="noindex"><title>週末バックテスト</title><style>{CSS}</style></head><body><main>')
    H.append(f'<p class="sub"><a href="../../">新高値デイリーボード</a> ／ <a href="../">週末バックテスト一覧</a></p>')
    H.append(f'<h1>週末バックテスト　{run_date.year}年{run_date.month}月{run_date.day}日</h1>')
    H.append(f'<p class="sub">対象週 {md(week_start)}〜{md(last_day)}（最終データ {last_day:%Y-%m-%d}）／ 検証期間 {hist_start:%Y-%m-%d}〜 ／ '
             f'検証銘柄 {n_enough:,}（株価{len(data):,}銘柄のうち、1年以上の正常なデータがあるもの）'
             + ('　<b class="up">※合成データによる動作確認</b>' if synthetic else '') + '</p>')
    H.append('<div class="panel concl">' + "".join(f"<p>{p}</p>" for p in concl) + '</div>')

    H.append('<h2>検証しているルール</h2><div class="rule">'
             '<div><b>買い</b>終値が52週（250営業日）の終値最高値を上回った翌営業日の寄付</div>'
             '<div><b>売り</b>終値が直近20営業日の安値を下回った翌営業日の寄付</div>'
             '<div><b>絞り込み条件</b>上昇トレンド＝終値＞25日線＞75日線＞200日線／出来高＝20日平均の1.5倍以上／売買代金＝1億円以上</div>'
             '<div><b>前提</b>1回100万円で購入、手数料・税・スリッページなし、同じ銘柄は保有中に買い増ししない</div></div>')

    # 今週のシグナル
    H.append(f'<h2>今週の新高値ブレイク銘柄（{n_ws}銘柄）</h2>')
    if ws.empty:
        H.append('<p class="note">今週は該当がありませんでした。</p>')
    else:
        def sig_table(df):
            t = ['<div class="tw"><table><tr><th class="l">コード</th><th class="l">銘柄</th><th class="l">市場</th><th>シグナル日</th><th>終値</th>'
                 '<th>出来高倍率</th><th>売買代金</th><th>トレンド</th><th>出来高</th><th>代金</th><th>買値（翌寄付）</th><th>金曜終値</th><th>損益</th><th>現在の損切ライン</th></tr>']
            for _, r in df.iterrows():
                i = info.get(r["code"], {})
                ret = None if r["entry_px"] is None or pd.isna(r["entry_px"]) else r["last"] / r["entry_px"] - 1
                mk = lambda b: '<span class="ok">○</span>' if b else '<span class="ng">×</span>'  # noqa: E731
                t.append(f'<tr{" class=hl" if r["combo"] else ""}><td class="l">{esc(r["code"])}</td><td class="l">{esc(i.get("name", ""))}</td>'
                         f'<td class="l">{esc(i.get("market", ""))}</td><td>{md(r["date"])}</td><td>{fmt_num(r["close"])}</td>'
                         f'<td>{fmt_num(r["vratio"], 1)}倍</td><td>{fmt_oku(r["value"])}</td><td>{mk(r["trend"])}</td><td>{mk(r["volume"])}</td><td>{mk(r["value_ok"])}</td>'
                         f'<td>{"月曜寄付" if ret is None else fmt_num(r["entry_px"])}</td><td>{fmt_num(r["last"])}</td>'
                         f'<td class="{cls_ret(ret)}">{fmt_pct(ret)}</td><td>{fmt_num(r["stop_now"])}</td></tr>')
            t.append('</table></div>')
            return "".join(t)
        top = ws[ws["combo"]]
        rest = ws[~ws["combo"]]
        H.append(f'<h3>3条件すべてを満たした銘柄（{len(top)}）</h3>')
        H.append(sig_table(top) if len(top) else '<p class="note">今週は該当なし。</p>')
        if len(rest):
            H.append(f'<details><summary>そのほかの{len(rest)}銘柄を表示（売買代金の大きい順）</summary>'
                     + sig_table(rest.sort_values("value", ascending=False)) + '</details>')
        H.append('<p class="note">損益は買値（シグナル翌日の寄付）から金曜終値まで。同じ週に複数回更新した銘柄は最初の日を表示。</p>')

        # 今週のパターン別
        H.append('<h3>パターン別：今週買った分の金曜時点の成績</h3><div class="tw"><table><tr><th class="l">パターン</th><th>今週の買い</th><th>平均損益</th><th>プラスの割合</th></tr>')
        for v, nm in VARIANTS:
            n, _, a, w = wk[v]
            H.append(f'<tr{" class=hl" if v == "combo" else ""}><td class="l">{esc(nm)}</td><td>{n}</td><td class="{cls_ret(a)}">{fmt_pct(a)}</td><td>{fmt_pct(w, False, 0)}</td></tr>')
        H.append('</table></div><p class="note">買った直後の数日分なので、ルールの良し悪しは下の過去検証で判断してください。</p>')

    # 保有・決済（3条件すべて）
    H.append('<h2>3条件すべて：いまの保有と今週の決済</h2>')

    def pos_table(df, closed_mode=False):
        t = ['<div class="tw"><table><tr><th class="l">コード</th><th class="l">銘柄</th><th>シグナル日</th><th>買い日</th><th>買値</th>'
             + ('<th>売り日</th><th>売値</th>' if closed_mode else '<th>金曜終値</th><th>損切ライン</th>')
             + '<th>損益</th><th>保有日数</th><th class="l">状態</th></tr>']
        for _, r in df.iterrows():
            i = info.get(r["code"], {})
            ret = r.get("ret")
            ret = None if ret is None or pd.isna(ret) else ret
            if closed_mode:
                mid = f'<td>{md(r["exit"])}</td><td>{fmt_num(r["exit_px"])}</td>'
            else:
                mid = f'<td>{fmt_num(r.get("last_px"))}</td><td>{fmt_num(r.get("stop_now"))}</td>'
            days = "—" if pd.isna(r.get("days")) else int(r["days"])
            t.append(f'<tr><td class="l">{esc(r["code"])}</td><td class="l">{esc(i.get("name", ""))}</td><td>{md(r["signal"])}</td>'
                     f'<td>{md(r["entry"])}</td><td>{fmt_num(r["entry_px"])}</td>{mid}<td class="{cls_ret(ret)}">{fmt_pct(ret)}</td>'
                     f'<td>{days}</td><td class="l">{status_pill(r["status"])}</td></tr>')
        t.append('</table></div>')
        return "".join(t)

    H.append(f'<h3>保有中・月曜の売買予定（{len(holding)}）</h3>')
    H.append(pos_table(holding) if len(holding) else '<p class="note">なし。</p>')
    H.append(f'<h3>今週決済（{len(week_closed)}）</h3>')
    H.append(pos_table(week_closed, True) if len(week_closed) else '<p class="note">なし。</p>')

    # 過去検証
    H.append(f'<h2>過去{HIST_YEARS}年の検証（{hist_start:%Y-%m-%d}〜）</h2>')
    H.append('<div class="tw"><table><tr><th class="l">パターン</th><th>売買回数</th><th>勝率</th><th>1回平均</th><th>中央値</th><th>勝ち平均</th><th>負け平均</th>'
             '<th>損益比 PF</th><th>平均保有日数</th><th>最大の勝ち</th><th>最大の負け</th><th>累計損益</th><th>最大同時保有</th></tr>')
    for v, nm in VARIANTS:
        s = st[v]
        if not s.get("n"):
            H.append(f'<tr><td class="l">{esc(nm)}</td><td colspan="12" class="l">決済済みのトレードなし</td></tr>')
            continue
        pf = "∞" if math.isinf(s["pf"]) else f'{s["pf"]:.2f}'
        H.append(f'<tr{" class=hl" if v == "combo" else ""}><td class="l">{esc(nm)}</td><td>{s["n"]:,}</td><td>{fmt_pct(s["win"], False, 0)}</td>'
                 f'<td class="{cls_ret(s["avg"])}">{fmt_pct(s["avg"])}</td><td class="{cls_ret(s["med"])}">{fmt_pct(s["med"])}</td>'
                 f'<td class="up">{fmt_pct(s["avg_win"])}</td><td class="down">{fmt_pct(s["avg_loss"])}</td><td>{pf}</td><td>{s["days"]:.0f}日</td>'
                 f'<td class="up">{fmt_pct(s["best"], True, 0)}</td><td class="down">{fmt_pct(s["worst"], True, 0)}</td>'
                 f'<td class="{cls_ret(s["pnl"])}">{s["pnl"] / 1e4:+,.0f}万円</td><td>{s["maxpos"]}銘柄</td></tr>')
    H.append('</table></div>')
    H.append(f'<p class="note">累計損益は「シグナルが出るたびに100万円ずつ買った」場合の合計。最大同時保有は、それを実行するのに必要だった同時保有数（×100万円が必要資金の目安）。</p>')

    H.append('<h3>累計損益の推移（決済日順）</h3><div class="chart">' + cum_chart(tr, hist_start, last_day) + '</div>')

    # 年別
    years = sorted({y for v in yearly for y in yearly[v]})
    if years:
        H.append('<h3>年別（買った年）</h3><div class="tw"><table><tr><th class="l">パターン</th>'
                 + "".join(f'<th>{y}年</th>' for y in years) + '</tr>')
        for v, nm in VARIANTS:
            cells = []
            for y in years:
                if y in yearly.get(v, {}):
                    n, w, a, p = yearly[v][y]
                    cells.append(f'<td><span class="{cls_ret(a)}">{fmt_pct(a)}</span><br><span class="note">{n}回・勝率{w * 100:.0f}%</span></td>')
                else:
                    cells.append('<td>—</td>')
            H.append(f'<tr{" class=hl" if v == "combo" else ""}><td class="l">{esc(nm)}</td>{"".join(cells)}</tr>')
        H.append('</table></div><p class="note">上段が1回平均の損益、下段が回数と勝率。直近の年はまだ保有中のものを含まないため、途中経過です。</p>')

    H.append('<h2>注意点</h2><ul class="note">'
             '<li>株価は Yahoo Finance（yfinance）の分割調整済みデータ。株探の表示と数円ずれることがあります。</li>'
             '<li>いま上場している銘柄だけで検証しているため、上場廃止になった銘柄の損失は含まれず、実際より成績がよく見えます（生存者バイアス）。</li>'
             '<li>寄付で必ず約定した前提です。ストップ高・ストップ安で約定しない日や、流動性の低い銘柄の滑りは考慮していません。</li>'
             '<li>手数料・税金は含みません。売買を推奨するものではなく、自分のルールの検証用です。</li></ul>')
    H.append(f'<p class="note">作成 {dt.datetime.now(JST):%Y-%m-%d %H:%M} JST（GitHub Actions による自動実行）</p>')
    H.append('</main></body></html>')

    summary = {
        "date": run_date.isoformat(), "week": [week_start.strftime("%Y-%m-%d"), last_day.strftime("%Y-%m-%d")],
        "signals": n_ws, "signals_combo": n_combo, "holding_combo": int(len(holding)),
        "stats": {v: {k: (None if isinstance(x, float) and (math.isnan(x) or math.isinf(x)) else x) for k, x in st[v].items()} for v, _ in VARIANTS},
        "summary": f"52週高値更新 {n_ws}銘柄（3条件すべて {n_combo}）。過去{HIST_YEARS}年・3条件すべて：勝率{fmt_pct(s_cb.get('win'), False, 0)}、1回平均{fmt_pct(s_cb.get('avg'))}、PF {s_cb.get('pf', float('nan')):.2f}。"
                   if s_cb.get("n") else f"52週高値更新 {n_ws}銘柄（3条件すべて {n_combo}）。",
        "synthetic": synthetic,
        "universe": int(len(uni)), "priced": int(len(data)), "tested": int(n_enough), "data_cut": int(n_cut),
        "extremes": [] if closed.empty else [
            {"code": r["code"], "variant": r["variant"], "entry": r["entry"].strftime("%Y-%m-%d"), "exit": r["exit"].strftime("%Y-%m-%d"),
             "entry_px": round(float(r["entry_px"]), 2), "exit_px": round(float(r["exit_px"]), 2), "ret": round(float(r["ret"]), 4)}
            for _, r in pd.concat([closed[closed["variant"] == "all"].nsmallest(5, "ret"), closed[closed["variant"] == "all"].nlargest(5, "ret")]).iterrows()],
    }
    return "\n".join(H), summary


def write_index():
    base = ROOT / "weekly"
    items = []
    for p in sorted(base.glob("*" + "/summary.json"), reverse=True):
        try:
            items.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
    lis = "".join(f'<li><a href="{esc(s["date"])}/">{esc(s["date"])}</a><span>{esc(s.get("summary", ""))}</span></li>' for s in items) \
        or '<li><span>まだレポートがありません</span></li>'
    latest = f'<a class="latest" href="{esc(items[0]["date"])}/">最新（{esc(items[0]["date"])}）を開く</a>' if items else ""
    page = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>週末バックテスト</title><style>{CSS}
ul{{list-style:none;padding:0;margin:0;background:var(--panel);border:1px solid var(--line);border-radius:10px}}
li{{display:flex;flex-wrap:wrap;gap:4px 16px;padding:10px 14px;border-bottom:1px solid var(--line)}}li:last-child{{border-bottom:0}}
li a{{font-weight:700}}li span{{color:var(--muted);font-size:.9rem}}
.latest{{display:inline-block;background:var(--accent);color:var(--panel);padding:10px 16px;border-radius:8px;text-decoration:none;font-weight:700;margin-bottom:20px}}
</style></head><body><main>
<p class="sub"><a href="../">新高値デイリーボード</a></p>
<h1>週末バックテスト</h1>
<p class="sub">毎週土曜の朝に自動更新。今週の52週高値ブレイク銘柄を「翌日寄付で買い・20日安値割れで売り」のルールで検証し、過去{HIST_YEARS}年の成績と比べます。売買を推奨するものではありません。</p>
{latest}<ul>{lis}</ul></main></body></html>"""
    (base / "index.html").write_text(page, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--date", help="レポートの日付（既定: 今日 JST）")
    ap.add_argument("--cache", default=os.environ.get("PRICE_CACHE", ""))
    ap.add_argument("--limit", type=int, default=0, help="銘柄数の上限（テスト用）")
    a = ap.parse_args()

    run_date = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(JST).date()
    if a.synthetic:
        uni, data = synthetic_prices(end=run_date - dt.timedelta(days=1))
    else:
        uni = load_universe()
        codes = uni["code"].tolist()
        if a.limit:
            codes = codes[:a.limit]
        start = run_date - dt.timedelta(days=int(365.25 * (HIST_YEARS + 1.5)))
        data = download_prices(codes, start, Path(a.cache) if a.cache else None)
        # 未来日付・当日の途中データを除く
        cutoff = pd.Timestamp(run_date)
        data = {c: d[d.index < cutoff] for c, d in data.items()}
        data = {c: d for c, d in data.items() if len(d)}
    if not data:
        print("株価データが取れませんでした", file=sys.stderr)
        sys.exit(1)

    page, summary = build_report(run_date, uni, data, a.synthetic)
    out = ROOT / "weekly" / run_date.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    write_index()
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
