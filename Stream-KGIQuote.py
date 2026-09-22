"""
Streams real-time tick + 5-minute Kbar data for a TW stock from KGI SUPER PY
(quoteapi.kgi.com.tw push channel) and continuously writes two JSON snapshot
files, so a webpage (which can't hold a kgisuperpy/native-DLL connection
itself) can poll those files instead - same pattern as Poll-LiveQuote.ps1.

The live push channel only sends what happens AFTER you subscribe - if you
start this at 10:00, you get nothing for 09:00-10:00. To fix that gap, this
script backfills "market open through now" on startup by calling the same
historical 1-min table kbar.py uses
('取得歷史分K(指定日期前)'), then aggregates it into N-minute bars and seeds
both JSON files before the live subscription takes over. Disable with
--no-backfill.

When the live ticks list hits MAX_TICKS (500) or the live bars dict hits
MAX_BARS (200), that full batch is archived as-is to output/6547_kgi_tick_N.json
/ output/6547_kgi_5min_N.json (N = 0, 1, 2, ... incrementing) instead of being
discarded, and the in-memory list/dict resets to empty. The webpage always
polls the fixed, unsuffixed filenames (output/6547_kgi_tick.json /
output/6547_kgi_5min.json), which only ever hold the current, still-filling
batch - so no history is lost, and the page doesn't need to know the archives
exist.

Run this and leave it running while the matching HTML page is open. Ctrl+C
to stop (unsubscribes cleanly first).

Credentials are resolved in this order: C:\Personal\KGI\kgi.txt (KEY=VALUE
lines, see kbar.py for the format) -> environment variables
(KGI_ID / KGI_PWD / KGI_ACCOUNT) -> interactive prompt.

Usage:
    python Stream-KGIQuote.py --stock 6547
    python Stream-KGIQuote.py --stock 6547 --minutes 5 --tick-file output/6547_kgi_tick.json --kbar-file output/6547_kgi_5min.json
    python Stream-KGIQuote.py --stock 6547 --no-backfill
"""
import argparse
import dataclasses
import getpass
import json
import os
import sys
import time

import kgisuperpy as kgi

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CRED_FILE = r"C:\Personal\KGI\kgi.txt"

MAX_TICKS = 500
MAX_BARS = 200


def load_cred_file(path):
    values = {}
    if not os.path.isfile(path):
        return values
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def get_credential(env_name, prompt, file_values, hidden=False):
    if env_name in file_values:
        return file_values[env_name]
    value = os.environ.get(env_name)
    if value:
        return value
    if hidden:
        return getpass.getpass(f"{prompt}: ")
    return input(f"{prompt}: ")


def to_row(obj):
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    row = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if callable(value):
            continue
        row[name] = value
    return row


def resolve_out_path(filename):
    path = os.path.join(SCRIPT_DIR, filename) if not os.path.isabs(filename) else filename
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def write_json(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def next_archive_index(path):
    """First unused N such that {stem}_{N}{ext} doesn't exist yet, so re-running
    the script later the same day continues numbering instead of overwriting
    earlier archives."""
    stem, ext = os.path.splitext(path)
    n = 0
    while os.path.exists(f"{stem}_{n}{ext}"):
        n += 1
    return n


def archive_path(path, index):
    stem, ext = os.path.splitext(path)
    return f"{stem}_{index}{ext}"


def manifest_path(path):
    stem, ext = os.path.splitext(path)
    return f"{stem}_manifest{ext}"


def write_manifest(path, count):
    """Tells the webpage how many archive files (_0..{count-1}) exist, so it
    can fetch exactly those instead of guessing/probing with 404s."""
    write_json(manifest_path(path), {"count": count})


def archive_overflow(items, max_size, path, start_index, base_payload, list_key):
    """Archives full max_size chunks off the front of `items` (oldest first)
    until at most max_size remain, so a backfill that already exceeds the
    cap on its own doesn't silently drop the earliest part - same
    no-data-lost guarantee as the live on_tick/on_kbar rotation.
    Returns (remaining_items, next_index)."""
    index = start_index
    while len(items) > max_size:
        batch, items = items[:max_size], items[max_size:]
        arc_path = archive_path(path, index)
        payload = dict(base_payload)
        payload["archivedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
        payload[list_key] = batch
        write_json(arc_path, payload)
        print(f"[backfill] 回補資料超過上限,封存 -> {arc_path}", file=sys.stderr)
        index += 1
    return items, index


def reset_if_stale(path, today_str, empty_payload):
    """If the live file already on disk wasn't last written today (e.g. left
    over from running this script yesterday), overwrite it with an empty
    payload immediately - so opening the page before backfill/the first tick
    arrives shows nothing instead of yesterday's stale data."""
    stale = True
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            stale = not str(data.get("updatedAt") or "").startswith(today_str)
        except Exception:
            stale = True
    if stale:
        write_json(path, empty_payload)
    return stale


def backfill_today(api, stock, minutes):
    """
    Fetch today's 1-min history so far (via the same table
    kbar.py uses) and turn it into:
      - tick_seed: one proxy "tick" per minute (real trade-by-trade tick
        history isn't available via Data.get, only OHLCV per minute)
      - bar_seed: dict of N-minute bars, keyed the same way the live kbar
        push keys them (by the bar's yyyyMMddHHMM timestamp), aggregated
        from consecutive 1-min rows starting at market open (09:01), which
        lines up with the live feed's clock-aligned 5-min boundaries since
        the session starts exactly on a clock boundary (09:00).
    Returns ([], {}) on any failure - the live subscription still works
    without a backfill, it just starts from "now" instead of market open.
    """
    today = time.strftime("%Y%m%d")
    try:
        df = api.Data.get("取得歷史分K(指定日期前)", stock, today, 1)
    except Exception as e:
        print(f"[backfill] 取得今日1分K失敗,略過回補: {e}", file=sys.stderr)
        return [], {}

    if df is None or len(df) == 0:
        print("[backfill] 今日目前無1分K資料(例如還沒開盤),略過回補", file=sys.stderr)
        return [], {}

    df = df.sort_values("hhmm").reset_index(drop=True)

    tick_seed = []
    for _, row in df.iterrows():
        tick_seed.append({
            "time": today + str(row["hhmm"]).zfill(4),
            "price": row["收盤價"], "volume": row["成交量"],
        })

    bar_seed = {}
    for start in range(0, len(df), minutes):
        chunk = df.iloc[start:start + minutes]
        if chunk.empty:
            continue
        dt = today + str(chunk.iloc[-1]["hhmm"]).zfill(4)
        bar_seed[dt] = {
            "datetime": dt,
            "open": float(chunk.iloc[0]["開盤價"]), "high": float(chunk["最高價"].max()),
            "low": float(chunk["最低價"].min()), "close": float(chunk.iloc[-1]["收盤價"]),
            "volume": float(chunk["成交量"].sum()),
        }

    print(f"[backfill] 補回 {len(tick_seed)} 筆分鐘資料、{len(bar_seed)} 根 {minutes} 分K(開盤~現在)", file=sys.stderr)
    return tick_seed, bar_seed


def main():
    parser = argparse.ArgumentParser(description="Stream KGI real-time tick + Kbar data to JSON files")
    parser.add_argument("--stock", required=True, help="Stock code, e.g. 6547")
    parser.add_argument("--minutes", type=int, default=5, choices=[1, 3, 5, 15, 30, 60],
                         help="Kbar interval in minutes (default 5)")
    parser.add_argument("--tick-file", default="output/6547_kgi_tick.json")
    parser.add_argument("--kbar-file", default="output/6547_kgi_5min.json")
    parser.add_argument("--simulation", action="store_true")
    parser.add_argument("--no-backfill", action="store_true",
                         help="Skip backfilling today's open-to-now history; start empty from whenever this runs")
    args = parser.parse_args()

    tick_path = resolve_out_path(args.tick_file)
    kbar_path = resolve_out_path(args.kbar_file)

    file_values = load_cred_file(CRED_FILE)
    person_id = get_credential("KGI_ID", "身分證字號 (login ID)", file_values)
    person_pwd = get_credential("KGI_PWD", "電子交易密碼", file_values, hidden=True)
    account = get_credential("KGI_ACCOUNT", "下單帳號", file_values)

    print("Logging in to KGI SUPER PY...", file=sys.stderr)
    api = kgi.login(person_id, person_pwd, args.simulation)
    api.set_Account(account)

    ticks = []
    bars = {}  # keyed by datetime so a still-forming bar gets updated in place, not duplicated
    tick_archive_index = next_archive_index(tick_path)
    kbar_archive_index = next_archive_index(kbar_path)

    today_str = time.strftime("%Y-%m-%d")
    if reset_if_stale(tick_path, today_str, {"symbol": args.stock, "updatedAt": None, "ticks": []}):
        print(f"[系統] {tick_path} 不是今天的資料,已清空", file=sys.stderr)
    if reset_if_stale(kbar_path, today_str, {"symbol": args.stock, "minutes": args.minutes, "updatedAt": None, "bars": []}):
        print(f"[系統] {kbar_path} 不是今天的資料,已清空", file=sys.stderr)

    if not args.no_backfill:
        tick_seed, bar_seed = backfill_today(api, args.stock, args.minutes)

        ticks.extend(tick_seed)
        ticks, tick_archive_index = archive_overflow(
            ticks, MAX_TICKS, tick_path, tick_archive_index,
            {"symbol": args.stock}, "ticks")

        bar_list = sorted(bar_seed.values(), key=lambda b: b["datetime"])
        bar_list, kbar_archive_index = archive_overflow(
            bar_list, MAX_BARS, kbar_path, kbar_archive_index,
            {"symbol": args.stock, "minutes": args.minutes}, "bars")
        bars.update({b["datetime"]: b for b in bar_list})

        if ticks or bars:
            write_json(tick_path, {"symbol": args.stock, "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"), "ticks": ticks})
            write_json(kbar_path, {
                "symbol": args.stock, "minutes": args.minutes,
                "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                "bars": sorted(bars.values(), key=lambda b: b["datetime"]),
            })

    # Tell the webpage how many archives already exist (from this run's
    # backfill, or from an earlier run today it hasn't seen yet).
    write_manifest(tick_path, tick_archive_index)
    write_manifest(kbar_path, kbar_archive_index)

    def on_tick(tick, *a, **kw):
        nonlocal tick_archive_index
        row = to_row(tick)
        ticks.append({"time": row.get("datetime", ""), "price": row.get("close"), "volume": row.get("volume")})

        if len(ticks) >= MAX_TICKS:
            arc_path = archive_path(tick_path, tick_archive_index)
            write_json(arc_path, {
                "symbol": args.stock, "archivedAt": time.strftime("%Y-%m-%d %H:%M:%S"), "ticks": ticks[:],
            })
            print(f"[TICK] 滿 {MAX_TICKS} 筆,封存 -> {arc_path},換下一檔", file=sys.stderr)
            tick_archive_index += 1
            ticks.clear()
            write_manifest(tick_path, tick_archive_index)

        write_json(tick_path, {
            "symbol": args.stock, "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"), "ticks": ticks,
        })
        print(f"[TICK] {row.get('datetime')} {row.get('close')}", file=sys.stderr)

    def on_kbar(kbar, *a, **kw):
        nonlocal kbar_archive_index
        row = to_row(kbar)
        bars[row.get("datetime")] = {
            "datetime": row.get("datetime"), "open": row.get("open"), "high": row.get("high"),
            "low": row.get("low"), "close": row.get("close"), "volume": row.get("volume"),
        }

        if len(bars) >= MAX_BARS:
            arc_path = archive_path(kbar_path, kbar_archive_index)
            write_json(arc_path, {
                "symbol": args.stock, "minutes": args.minutes,
                "archivedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                "bars": sorted(bars.values(), key=lambda b: b["datetime"]),
            })
            print(f"[KBAR] 滿 {MAX_BARS} 根,封存 -> {arc_path},換下一檔", file=sys.stderr)
            kbar_archive_index += 1
            bars.clear()
            write_manifest(kbar_path, kbar_archive_index)

        bar_list = sorted(bars.values(), key=lambda b: b["datetime"])
        write_json(kbar_path, {
            "symbol": args.stock, "minutes": args.minutes,
            "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"), "bars": bar_list,
        })
        print(f"[KBAR] {row.get('datetime')} O{row.get('open')} H{row.get('high')} L{row.get('low')} C{row.get('close')}", file=sys.stderr)

    print(f"Subscribing to tick + {args.minutes}-min kbar for {args.stock}...", file=sys.stderr)
    api.Quote.set_cb_tick(on_tick)
    api.Quote.set_cb_kbar(on_kbar)
    api.Quote.subscribe_tick(args.stock)
    api.Quote.subscribe_kbar(args.stock, minute=args.minutes)

    print(f"Streaming. Writing {tick_path} and {kbar_path}. Ctrl+C to stop.", file=sys.stderr)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        print("Unsubscribing...", file=sys.stderr)
        api.Quote.unsubscribe_all()


if __name__ == "__main__":
    main()
