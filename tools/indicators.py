#!/usr/bin/env python3
"""持ち株の指標を毎日同じルールで計算する（AIに計算させないための部品）。

使い方:
  python3 tools/indicators.py work/D/raw/8306_daily.csv --stop 3541 [--prev-meta daily/<前営業日>/meta.json --code 8306]

入力 CSV（株探の日足を書き出したもの。並び順は問わない）:
  date,open,high,low,close,volume
  2026-10-23,3360,3296,3190,3295,41234500

出力（標準出力に JSON）:
  移動平均（5/25/75/200日）、52週高値（高値ベースと終値ベース）、20日安値（安値ベースと終値ベース）、
  トレンド判定（株価>25日>75日>200日）、自分の売買ルールのシグナル、損切ラインまでの距離、
  ライン割れの連続日数、meta.json 用の closes 文字列（直近120営業日・新しい順）。
"""
import argparse
import csv
import json
import sys


def num(s):
    s = str(s).replace(",", "").strip()
    return float(s) if s not in ("", "-", "－") else None


def fmt(x):
    if x is None:
        return None
    return int(x) if float(x).is_integer() else round(x, 1)


def load(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            c = num(r["close"])
            if c is None:
                continue
            rows.append({
                "date": r["date"].strip(),
                "open": num(r.get("open", "")),
                "high": num(r.get("high", "")) or c,
                "low": num(r.get("low", "")) or c,
                "close": c,
                "volume": num(r.get("volume", "")),
            })
    rows.sort(key=lambda r: r["date"])  # 古い順
    if not rows:
        sys.exit("日足が空です: " + path)
    return rows


def ma(closes, n):
    return sum(closes[-n:]) / n if len(closes) >= n else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--stop", type=float, required=True, help="前営業日から引き継いだ損切ライン")
    args = ap.parse_args()

    rows = load(args.csv)
    closes = [r["close"] for r in rows]
    t = rows[-1]
    prev = rows[:-1]

    y = rows[-251:]  # 約52週（当日を含む）
    hi_row = max(y, key=lambda r: r["high"])
    hi_close_row = max(y, key=lambda r: r["close"])
    last20 = rows[-21:-1]  # 当日を含まない直近20営業日
    low20 = min(last20, key=lambda r: r["low"]) if last20 else None
    low20c = min(last20, key=lambda r: r["close"]) if last20 else None

    m5, m25, m75, m200 = (ma(closes, n) for n in (5, 25, 75, 200))
    uptrend = None
    if None not in (m25, m75, m200):
        uptrend = t["close"] > m25 > m75 > m200

    prev_year = [r["close"] for r in prev[-250:]]
    new_high_close = bool(prev_year) and t["close"] > max(prev_year)
    break_low20_close = low20c is not None and t["close"] < low20c["close"]

    below = 0
    for r in reversed(rows):
        if r["close"] < args.stop:
            below += 1
        else:
            break

    out = {
        "date": t["date"],
        "close": fmt(t["close"]),
        "change": fmt(t["close"] - prev[-1]["close"]) if prev else None,
        "change_pct": round((t["close"] / prev[-1]["close"] - 1) * 100, 2) if prev else None,
        "ma": {"5": fmt(round(m5)) if m5 else None, "25": fmt(round(m25)) if m25 else None,
               "75": fmt(round(m75)) if m75 else None, "200": fmt(round(m200)) if m200 else None},
        "high52w": {"price": fmt(hi_row["high"]), "date": hi_row["date"],
                    "to_high_pct": round((hi_row["high"] / t["close"] - 1) * 100, 1)},
        "high52w_close": {"price": fmt(hi_close_row["close"]), "date": hi_close_row["date"]},
        "low20": {"price": fmt(low20["low"]), "date": low20["date"]} if low20 else None,
        "low20_close": {"price": fmt(low20c["close"]), "date": low20c["date"]} if low20c else None,
        "uptrend": uptrend,
        "signals": {
            "new_52w_high_close": new_high_close,      # 買いルール：52週高値を終値で更新
            "break_20d_low_close": break_low20_close,  # 売りルール：直近20日安値を終値で割った
        },
        "stop": {
            "line": fmt(args.stop),
            "below": t["close"] < args.stop,
            "distance_pct": round((t["close"] / args.stop - 1) * 100, 1),
            "consecutive_days_below": below,
        },
        "bars": len(rows),
        "closes": " ".join(
            f'{r["date"][5:7]}/{r["date"][8:10]}:{fmt(r["close"])}' for r in reversed(rows[-120:])
        ),
    }
    if len(rows) < 200:
        out["warning"] = f"日足が{len(rows)}本しかないため、200日線などが計算できない場合があります"
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == "__main__":
    main()
