#!/usr/bin/env python3
"""投信ダッシュボード 日次更新スクリプト

データ元: 投資信託協会「投信総合検索ライブラリー」(toushin-lib.fwg.ne.jp)
使い方:  python3 update.py --hist hist.json --out OUTDIR [--max-backfill N]

出力 (OUTDIR):
  funds.json        一覧・検索・ランキング用の全ファンド要約
  series/NN.json    詳細画面用の直近日次データ (32 分割)
  hist.json         次回更新用の履歴 (基準価額・純資産)
資金流入の推計: flow_t = NA_t - NA_{t-1} * (NAV_t + 分配金_t) / NAV_{t-1}  (百万円)
"""
import argparse, json, os, sys, time, zlib, datetime as dt
import urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor

BASE = "https://toushin-lib.fwg.ne.jp/FdsWeb"
UA = {"User-Agent": "Mozilla/5.0 (fund-dashboard daily updater)",
      "Content-Type": "application/x-www-form-urlencoded"}
KEEP_DAYS = 45      # 履歴の保持日数(営業日)
SERIES_DAYS = 30    # 詳細画面に出す日数
SHARDS = 32


def http(url, data=None, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=UA)
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read()
        except Exception as e:  # noqa
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def fetch_page(start):
    body = urllib.parse.urlencode({"s_kensakuKbn": "1", "startNo": str(start),
                                   "draw": "1", "searchBtnClickFlg": "true"}).encode()
    j = json.loads(http(BASE + "/FDST999900/fundDataSearch", body))
    return j["searchResultInfo"]


def fetch_all_funds():
    first = fetch_page(0)
    total = int(first["recordsTotal"])
    recs = list(first["resultInfoMapList"])
    starts = list(range(20, total, 20))
    with ThreadPoolExecutor(4) as ex:
        for info in ex.map(fetch_page, starts):
            recs.extend(info["resultInfoMapList"])
    return total, recs


def fetch_csv(isin, assoc):
    q = urllib.parse.urlencode({"isinCd": isin, "associFundCd": assoc})
    raw = http(BASE + "/FDST030000/csv-file-download?" + q)
    for enc in ("utf-8-sig", "cp932"):
        try:
            txt = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    rows = []
    for line in txt.splitlines()[1:]:
        p = line.split(",")
        if len(p) < 3 or not p[1]:
            continue
        d = p[0].replace("年", "-").replace("月", "-").replace("日", "")
        try:
            rows.append([d, float(p[1]), float(p[2] or 0), float(p[3] or 0) if len(p) > 3 and p[3] else 0.0])
        except ValueError:
            continue
    return rows[-KEEP_DAYS:]


def num(v):
    try:
        return None if v in (None, "", "-") else float(v)
    except (TypeError, ValueError):
        return None


def flows(h):
    out = []
    for i in range(1, len(h)):
        d, nav, na, div = h[i]
        _, nav0, na0, _ = h[i - 1]
        f = na - na0 * ((nav + div) / nav0) if nav0 else 0.0
        out.append([d, round(f, 1)])
    return out


def window_sum(fl, last, days=None, months=None):
    if not fl:
        return None
    ld = dt.date.fromisoformat(last)
    if days:
        start = ld - dt.timedelta(days=days)
    else:
        y, m = ld.year, ld.month - months
        while m <= 0:
            m += 12; y -= 1
        start = dt.date(y, m, min(ld.day, 28))
    s = sum(f for d, f in fl if dt.date.fromisoformat(d) > start)
    return round(s, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hist", default="hist.json")
    ap.add_argument("--out", default="out")
    ap.add_argument("--max-backfill", type=int, default=100000)
    a = ap.parse_args()
    os.makedirs(os.path.join(a.out, "series"), exist_ok=True)

    hist = {}
    if os.path.exists(a.hist):
        try:
            hist = json.load(open(a.hist, encoding="utf-8")).get("h", {})
        except Exception as e:
            print("hist load failed:", e, file=sys.stderr)

    t0 = time.time()
    total, recs = fetch_all_funds()
    print(f"search: {len(recs)}/{total} funds in {time.time()-t0:.0f}s", file=sys.stderr)
    if len(recs) < total * 0.95:
        sys.exit("too few records fetched; aborting so the published data stays intact")

    funds = {}
    for x in recs:
        fid = f'{x.get("isinCd")}_{x.get("associFundCd")}'
        funds[fid] = x

    # 履歴の更新: スナップショットを追加
    need_csv = []
    for fid, x in funds.items():
        h = hist.get(fid, [])
        sd = (x.get("standardDate") or "")[:10]
        nav, na = num(x.get("standardPrice")), num(x.get("totalNetAssets"))
        if len(h) < 6:
            need_csv.append(fid)
            continue
        if sd and nav and na is not None and sd > h[-1][0]:
            div = 0.0
            for di in x.get("dividendInfo") or []:
                if (di.get("setlDate") or "")[:10] == sd:
                    div = num(di.get("dividend")) or 0.0
            h.append([sd, nav, na, div])
            hist[fid] = h[-KEEP_DAYS:]

    # 新規・履歴不足のファンドは CSV で補完
    need_csv = need_csv[: a.max_backfill]
    if need_csv:
        t1 = time.time()
        def job(fid):
            isin, assoc = fid.split("_", 1)
            try:
                return fid, fetch_csv(isin, assoc)
            except Exception as e:
                return fid, None
        done = 0
        with ThreadPoolExecutor(4) as ex:
            for fid, rows in ex.map(job, need_csv):
                done += 1
                if rows:
                    hist[fid] = rows
        print(f"csv backfill: {done} funds in {time.time()-t1:.0f}s", file=sys.stderr)

    # 出力
    asof = max(((x.get("standardDate") or "")[:10] for x in funds.values()), default="")
    cols = ["id", "name", "short", "nick", "co", "na", "nav", "navChg", "d1", "w1", "m1",
            "mOff", "r1y", "fee", "tsumitate", "growth", "date", "ests", "url"]
    rows, series = [], {}
    for fid, x in funds.items():
        h = hist.get(fid, [])
        fl = flows(h)
        last = h[-1][0] if h else None
        d1 = fl[-1][1] if fl and last and fl[-1][0] == last else None
        w1 = window_sum(fl, last, days=7) if fl else None
        m1 = window_sum(fl, last, months=1) if fl else None
        nick = (x.get("fundNkNm") or "").strip()
        rows.append([
            fid,
            (x.get("fundNm") or "").strip(),
            (x.get("fundStNm") or "").strip(),
            nick,
            (x.get("entrustCmpNm") or "").strip(),
            num(x.get("totalNetAssets")),
            num(x.get("standardPrice")),
            num(x.get("stdCostPointFrmDayBfrRt")),
            d1, w1, m1,
            num(x.get("monthlyCancelCreateVal")),
            num(x.get("standardPriceRa1y")),
            num(x.get("trustReward")),
            1 if x.get("nisaFlg") == "1" else 0,
            1 if x.get("nisaGrowthFlg") == "1" else 0,
            (x.get("standardDate") or "")[:10],
            (x.get("establishedDate") or "")[:10],
            x.get("reportUrl") or "",
        ])
        fmap = dict(fl)
        series[fid] = [[d, nav, na, fmap.get(d)] for d, nav, na, _ in h[-SERIES_DAYS:]]

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")
    meta = {"asof": asof, "updated": now, "count": len(rows), "cols": cols, "rows": rows,
            "source": "投資信託協会 投信総合検索ライブラリー"}
    with open(os.path.join(a.out, "funds.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, separators=(",", ":"))
    shards = [{} for _ in range(SHARDS)]
    for fid, s in series.items():
        shards[zlib.crc32(fid.encode()) % SHARDS][fid] = s
    for i, sh in enumerate(shards):
        with open(os.path.join(a.out, "series", f"{i:02d}.json"), "w", encoding="utf-8") as f:
            json.dump(sh, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(a.out, "hist.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": now, "h": {k: v for k, v in hist.items() if k in funds}},
                  f, ensure_ascii=False, separators=(",", ":"))
    print(f"wrote {len(rows)} funds, asof {asof}, total {time.time()-t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
