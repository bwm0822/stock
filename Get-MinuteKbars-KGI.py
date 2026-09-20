"""
Fetch historical minute-level Kbars for a TW stock via KGI SUPER PY (kgisuperpy).

Prerequisites:
    pip install kgisuperpy pandas

Credentials are resolved in this order:
    1. C:\Personal\KGI\kgi.txt, if present. Format (one KEY=VALUE
       per line):
           KGI_ID=A123456789
           KGI_PWD=your_password
           KGI_ACCOUNT=1234567
    2. Environment variables KGI_ID / KGI_PWD / KGI_ACCOUNT.
    3. Interactive prompt (password input is hidden).

kgi.txt holds a plaintext password, so keep it out of anywhere that syncs or
gets shared (cloud drives, git, etc.).

This script ONLY calls the read-only market-data function (api.Data.get with
a historical-minute-Kbar table). It never calls any order-placement
function. Double-check before adding any trading logic, since this connects
to KGI's PRODUCTION environment.

Confirmed via api.Data.get_table() (this account's live, authoritative
catalog) that the real minute-Kbar table for this account is:
    '取得歷史分K(指定日期前)'  ->  args: (symbol, date "yyyyMMdd", minutes)
    minutes can be 1/3/5/15/30/60. For minutes=1, 'date' can go back up to
    ~60 trading days. Despite the "指定日期前" (before the specified date)
    name, empirically each call only returns that single day's data, not a
    rolling multi-day window - so this script calls it once per calendar
    day in the requested range (skipping non-trading days) and stitches the
    per-day results together.

'取得歷史1分K(指定起訖日)' and '取得歷史1分K(指定日期前5天)' - the names
this script originally tried, based on generic web examples - do NOT appear
in this account's catalog at all, which is why they 403'd.

Usage:
    python Get-MinuteKbars-KGI.py --stock 2330 --start 2026-08-18 --end 2026-09-18 --out 2330_minute.csv
    python Get-MinuteKbars-KGI.py --stock 2330 --start 2026-08-18 --end 2026-09-18 --minutes 5 --out 2330_5min.csv
"""
import argparse
import getpass
import io
import os
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta

import kgisuperpy as kgi
import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CRED_FILE = r"C:\Personal\KGI\kgi.txt"
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")


def resolve_out_path(filename):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    return os.path.join(OUTPUT_DIR, filename)


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


def to_yyyymmdd(date_str):
    return date_str.replace("-", "")


def reference_dates(start_ymd, end_ymd, step_days=1):
    # Empirically, '取得歷史分K(指定日期前)' only returns that single day's
    # data per call (not a multi-day rolling window despite the name), so
    # every calendar day must be requested individually; non-trading days
    # are skipped by the caller when the server rejects them.
    start = datetime.strptime(start_ymd, "%Y%m%d")
    end = datetime.strptime(end_ymd, "%Y%m%d")
    dates = []
    cursor = start
    while cursor <= end:
        dates.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=step_days)
    if dates[-1] != end.strftime("%Y%m%d"):
        dates.append(end.strftime("%Y%m%d"))
    return dates


def fetch_chunks(api, stock, start_ymd, end_ymd, minutes=1):
    frames = []
    for ref_date in reference_dates(start_ymd, end_ymd):
        print(f"  Fetching window before {ref_date} (minutes={minutes})...", file=sys.stderr)
        try:
            chunk_df = api.Data.get("取得歷史分K(指定日期前)", stock, ref_date, minutes)
        except ValueError as e:
            # Most commonly: ref_date isn't a trading day (weekend/holiday).
            print(f"  Skipped {ref_date}: {e}", file=sys.stderr)
            continue
        if chunk_df is not None and len(chunk_df) > 0:
            frames.append(chunk_df)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates()

    date_col = next((c for c in df.columns if "date" in c.lower() or "time" in c.lower() or c in ("ts",)), None)
    if date_col:
        parsed = pd.to_datetime(df[date_col].astype(str), errors="coerce")
        range_start = pd.to_datetime(start_ymd)
        range_end = pd.to_datetime(end_ymd) + pd.Timedelta(days=1)
        df = df[(parsed >= range_start) & (parsed < range_end)]
        df = df.assign(_sort_key=parsed[df.index]).sort_values("_sort_key").drop(columns="_sort_key").reset_index(drop=True)
    return df


def main():
    parser = argparse.ArgumentParser(description="Fetch minute-level Kbars from KGI SUPER PY")
    parser.add_argument("--stock", help="Stock code, e.g. 2330")
    parser.add_argument("--start", help="Start date, yyyy-MM-dd")
    parser.add_argument("--end", help="End date, yyyy-MM-dd")
    parser.add_argument("--out", help="Optional CSV filename, saved under output\\")
    parser.add_argument("--simulation", action="store_true", help="Use simulation environment instead of production")
    parser.add_argument("--minutes", type=int, default=1, choices=[1, 3, 5, 15, 30, 60],
                         help="Kbar interval in minutes (default 1)")
    parser.add_argument("--list-tables", action="store_true",
                         help="Instead of fetching data, print this account's live Data.get_table() "
                              "catalog (which tables/args are actually available) and exit")
    args = parser.parse_args()

    if not args.list_tables and (not args.stock or not args.start or not args.end):
        parser.error("--stock/--start/--end are required unless --list-tables is given")

    file_values = load_cred_file(CRED_FILE)
    person_id = get_credential("KGI_ID", "身分證字號 (login ID)", file_values)
    person_pwd = get_credential("KGI_PWD", "電子交易密碼", file_values, hidden=True)
    account = get_credential("KGI_ACCOUNT", "下單帳號", file_values)

    print("Logging in to KGI SUPER PY...", file=sys.stderr)
    api = kgi.login(person_id, person_pwd, args.simulation)
    api.set_Account(account)

    if args.list_tables:
        # api.Data.get_table() prints the catalog itself and returns None -
        # there's no structured return value to export, so capture what it
        # actually prints instead.
        buf = io.StringIO()
        with redirect_stdout(buf):
            api.Data.get_table()
        catalog_text = buf.getvalue()
        print(catalog_text)
        if args.out:
            out_path = resolve_out_path(args.out)
            with open(out_path, "w", encoding="utf-8-sig") as f:
                f.write(catalog_text)
            print(f"Exported to {out_path}", file=sys.stderr)
        return

    start_ymd = to_yyyymmdd(args.start)
    end_ymd = to_yyyymmdd(args.end)

    print(f"Fetching {args.minutes}-min kbars for {args.stock} from {start_ymd} to {end_ymd}...", file=sys.stderr)
    df = fetch_chunks(api, args.stock, start_ymd, end_ymd, minutes=args.minutes)

    print(df)

    if args.out:
        out_path = resolve_out_path(args.out)
        df.to_csv(out_path, index=False, encoding="utf-8-sig")
        print(f"Exported to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
