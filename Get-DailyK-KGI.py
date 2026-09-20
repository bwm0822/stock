"""
Fetch daily/weekly/monthly Kbars (with technical indicators + 籌碼/chip data)
for a TW stock via KGI SUPER PY (kgisuperpy).

Unlike the minute-Kbar table, these tables only take a single 'symbol' arg
and return this account's full available history for that stock in one call
- no date range or per-day looping needed. Confirmed via api.Data.get_table():

    '日K/技術指標/價量資料-單檔股票多個區間'         -> args: (symbol)
    '週K/技術指標/價量/籌碼資料-單檔股票多個區間'     -> args: (symbol)
    '月K/技術指標/價量/籌碼資料-單檔股票多個區間'     -> args: (symbol)
    '還原日K價量資料(個股、ETF、大盤)-單檔股票多個區間' -> args: (symbol)

Credentials are resolved in this order: C:\Personal\KGI\kgi.txt (KEY=VALUE
lines, see Get-MinuteKbars-KGI.py for the format) -> environment variables
(KGI_ID / KGI_PWD / KGI_ACCOUNT) -> interactive prompt.

Usage:
    python Get-DailyK-KGI.py --stock 6547 --out 6547_daily.csv
    python Get-DailyK-KGI.py --stock 6547 --period week --out 6547_weekly.csv
"""
import argparse
import getpass
import os
import sys

import kgisuperpy as kgi

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CRED_FILE = r"C:\Personal\KGI\kgi.txt"
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

TABLE_BY_PERIOD = {
    "day": "日K/技術指標/價量資料-單檔股票多個區間",
    "week": "週K/技術指標/價量/籌碼資料-單檔股票多個區間",
    "month": "月K/技術指標/價量/籌碼資料-單檔股票多個區間",
    "adjusted_day": "還原日K價量資料(個股、ETF、大盤)-單檔股票多個區間",
}


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


def main():
    parser = argparse.ArgumentParser(description="Fetch daily/weekly/monthly Kbars from KGI SUPER PY")
    parser.add_argument("--stock", required=True, help="Stock code, e.g. 6547")
    parser.add_argument("--period", choices=list(TABLE_BY_PERIOD), default="day",
                         help="day (default) / week / month / adjusted_day")
    parser.add_argument("--out", help="Optional CSV filename, saved under output\\")
    parser.add_argument("--simulation", action="store_true", help="Use simulation environment instead of production")
    args = parser.parse_args()

    file_values = load_cred_file(CRED_FILE)
    person_id = get_credential("KGI_ID", "身分證字號 (login ID)", file_values)
    person_pwd = get_credential("KGI_PWD", "電子交易密碼", file_values, hidden=True)
    account = get_credential("KGI_ACCOUNT", "下單帳號", file_values)

    print("Logging in to KGI SUPER PY...", file=sys.stderr)
    api = kgi.login(person_id, person_pwd, args.simulation)
    api.set_Account(account)

    table = TABLE_BY_PERIOD[args.period]
    print(f"Fetching {args.period} Kbars for {args.stock} via '{table}'...", file=sys.stderr)
    df = api.Data.get(table, args.stock)

    print(f"Columns: {list(df.columns)}", file=sys.stderr)
    print(df)

    if args.out:
        out_path = resolve_out_path(args.out)
        df.to_csv(out_path, index=False, encoding="utf-8-sig")
        print(f"Exported to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
