"""
Fetch historical minute-level Kbars for a TW stock via KGI SUPER PY (kgisuperpy).

Has both a GUI and a CLI:
    - Run with no arguments (e.g. double-click, or `python kbar.py`)
      to launch a Tkinter GUI: fill in stock/date range/minutes, click 開始下載,
      watch progress in the log pane. Credential prompts (if kgi.txt/env vars
      aren't set) appear as GUI dialogs instead of a terminal prompt.
    - Run with any command-line argument to use the original CLI (see Usage
      below) - useful for scripting/automation.

Prerequisites:
    pip install kgisuperpy pandas
    pip install tkcalendar   (GUI mode only - gives the date-picker fields)

Credentials are resolved in this order:
    1. C:\\Personal\\KGI\\kgi.txt, if present. Format (one KEY=VALUE
       per line):
           KGI_ID=A123456789
           KGI_PWD=your_password
           KGI_ACCOUNT=1234567
    2. Environment variables KGI_ID / KGI_PWD / KGI_ACCOUNT.
    3. Interactive prompt - terminal input (CLI) or a GUI dialog (GUI mode),
       password input hidden either way.

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

Every run also fetches the full daily-K history for --stock and saves it as
output/{stock}_daily.csv - this is the same table/call Get-DailyK-KGI.py
uses ('日K/技術指標/價量資料-單檔股票多個區間', args: (symbol) only, returns
this account's full available history in one call, no date range needed).
Always on (not optional) because stocks.html wants both {stock}_daily.csv
(for the main chart) and {stock}_kbar.csv (for intraday drill-down + the
strategy backtester) for a stock to show up in its dropdown.

Re-running for a stock you've already fetched only requests the missing
days from KGI (skips ones already in {stock}_kbar.csv) and merges the
result into the existing file instead of overwriting it.

Usage:
    python kbar.py
        (no args -> launches the GUI)
    python kbar.py --stock 2330 --start 2026-08-18 --end 2026-09-18 --out 2330_minute.csv
    python kbar.py --stock 2330 --start 2026-08-18 --end 2026-09-18 --minutes 5 --out 2330_5min.csv
"""
import argparse
import getpass
import io
import json
import os
import queue
import re
import sys
import threading
import tkinter as tk
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta
from tkinter import messagebox, scrolledtext, simpledialog, ttk

import kgisuperpy as kgi
import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CRED_FILE = r"C:\Personal\KGI\kgi.txt"
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
MANIFEST_FILENAME = "stocks_manifest.json"
DAILY_TABLE = "日K/技術指標/價量資料-單檔股票多個區間"


def resolve_out_path(filename):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    return os.path.join(OUTPUT_DIR, filename)


def update_stocks_manifest(csv_filename):
    # stocks.html's stock-picker dropdown reads this file to find
    # out which stock codes have data available. Only filenames matching the
    # {code}_daily.csv / {code}_kbar.csv convention it actually looks for are
    # recorded - a custom --out name that doesn't match is silently skipped
    # since the chart page wouldn't find it under that name anyway.
    m = re.match(r"^(.+)_(daily|kbar)\.csv$", os.path.basename(csv_filename), re.IGNORECASE)
    if not m:
        return
    stock, kind = m.group(1), m.group(2).lower()
    manifest_path = resolve_out_path(MANIFEST_FILENAME)
    manifest = {}
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (json.JSONDecodeError, OSError):
            manifest = {}
    entry = manifest.get(stock, {})
    entry[kind] = True
    manifest[stock] = entry
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)


def first_kbar_date(stock):
    # Earliest date already present in output/{stock}_kbar.csv, so picking a
    # known stock from the GUI dropdown can jump the start-date picker to
    # match what's already been fetched. Returns None if there's no kbar
    # file yet, or it has no usable 日期 column/rows.
    path = resolve_out_path(f"{stock}_kbar.csv")
    if not os.path.isfile(path):
        return None
    try:
        df = pd.read_csv(path, usecols=["日期"])
    except (OSError, ValueError, pd.errors.EmptyDataError, pd.errors.ParserError, KeyError):
        return None
    # format="mixed": don't let pandas infer+cache a single format from the
    # first few rows and then choke on other rows that don't match it - see
    # the fetch_chunks() note about why 日期 can legitimately be a mix of
    # "yyyy-mm-dd" and "yyyy-mm-dd HH:MM:SS" if the file predates that fix.
    dates = pd.to_datetime(df["日期"], format="mixed", errors="coerce").dropna()
    if dates.empty:
        return None
    return dates.min().date()


def list_known_stocks():
    # Stock codes that already have a {code}_daily.csv or {code}_kbar.csv on
    # disk (per the manifest), offered as dropdown choices in the GUI's stock
    # field so re-downloading a stock you've already fetched before doesn't
    # require retyping the code.
    manifest_path = resolve_out_path(MANIFEST_FILENAME)
    if not os.path.isfile(manifest_path):
        return []
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    return sorted(manifest.keys())


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


def get_credential_gui(env_name, prompt, file_values, parent, hidden=False):
    if env_name in file_values:
        return file_values[env_name]
    value = os.environ.get(env_name)
    if value:
        return value
    kwargs = {"show": "*"} if hidden else {}
    value = simpledialog.askstring("KGI 登入", prompt, parent=parent, **kwargs)
    if not value:
        raise RuntimeError("已取消輸入帳密,無法登入")
    return value


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


def existing_kbar_dates(out_path):
    # yyyymmdd dates already saved in an existing kbar CSV, so a re-download
    # can skip re-requesting days it already has from KGI's API entirely
    # (not just de-dupe them after the fact) - fewer API calls, faster
    # re-runs. Returns an empty set if there's no file yet or it can't be read.
    if not os.path.isfile(out_path):
        return set()
    try:
        df = pd.read_csv(out_path, usecols=["日期"])
    except (OSError, ValueError, pd.errors.EmptyDataError, pd.errors.ParserError, KeyError):
        return set()
    dates = pd.to_datetime(df["日期"], format="mixed", errors="coerce").dropna()
    return set(dates.dt.strftime("%Y%m%d"))


def fetch_chunks(api, stock, start_ymd, end_ymd, minutes=1, skip_dates=None):
    skip_dates = skip_dates or set()
    frames = []
    for ref_date in reference_dates(start_ymd, end_ymd):
        if ref_date in skip_dates:
            print(f"  Already have {ref_date} locally, skipping", file=sys.stderr)
            continue
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

    if "日期" in df.columns:
        # kgisuperpy hands back 日期 as a real Timestamp column, which
        # to_csv() would otherwise render as "2026-09-22 00:00:00" - stocks.html
        # uses this column as a plain "yyyy-mm-dd" string (object key, <input
        # type="date"> value, lexicographic sort), so normalize it here once,
        # at the single choke point both the CLI and GUI go through.
        df["日期"] = pd.to_datetime(df["日期"], format="mixed").dt.strftime("%Y-%m-%d")
    return df


def fetch_daily(api, stock):
    # Same table Get-DailyK-KGI.py uses: takes only `symbol`, no date range -
    # returns this account's full available daily history in one call.
    return api.Data.get(DAILY_TABLE, stock)


def merge_kbar_csv(out_path, new_df):
    # Re-running a kbar download into the same file merges with whatever's
    # already there instead of overwriting: the union of old + new rows
    # fills in any gaps the old file had, and de-duping on 日期+hhmm (keeping
    # the newly-fetched row on overlap) removes redundant rows for days
    # that were fetched both times, so the file never grows duplicates.
    if not os.path.isfile(out_path):
        return new_df
    try:
        # hhmm must round-trip as a 4-char zero-padded string ("0901") - left
        # to type inference, pandas reads a column of all-digit strings back
        # as int64 and silently drops the leading zero ("0901" -> 901),
        # which would both break the dedup match below and corrupt the file
        # (downstream code like stocks.html slices hhmm assuming 4 digits).
        existing_df = pd.read_csv(out_path, dtype={"hhmm": str})
    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError, ValueError):
        return new_df
    if existing_df.empty:
        return new_df
    if "日期" in existing_df.columns:
        # Defensive: normalize even if the existing file predates the
        # fetch_chunks() fix and still has a "yyyy-mm-dd HH:MM:SS" mix, so a
        # merge doesn't propagate that inconsistency forward again.
        existing_df["日期"] = pd.to_datetime(existing_df["日期"], format="mixed").dt.strftime("%Y-%m-%d")
    if "hhmm" in existing_df.columns:
        existing_df["hhmm"] = existing_df["hhmm"].str.zfill(4)
    if "hhmm" in new_df.columns:
        new_df = new_df.copy()
        new_df["hhmm"] = new_df["hhmm"].astype(str).str.zfill(4)
    merged = pd.concat([existing_df, new_df], ignore_index=True)
    dedup_cols = [c for c in ("日期", "hhmm") if c in merged.columns]
    merged = merged.drop_duplicates(subset=dedup_cols or None, keep="last")
    if "日期" in merged.columns and "hhmm" in merged.columns:
        merged = merged.sort_values(["日期", "hhmm"]).reset_index(drop=True)
    return merged


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class QueueWriter(io.TextIOBase):
    """Redirects print()/stderr output from the worker thread into a
    thread-safe queue; Tkinter widgets can only be touched from the main
    thread, so the GUI polls this queue instead of writing to it directly."""

    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(s)
        return len(s)

    def flush(self):
        pass


class App:
    def __init__(self, root):
        # Local import: tkcalendar is only needed for the GUI's date pickers,
        # so the CLI path (module-level imports only) doesn't require it.
        from tkcalendar import DateEntry

        self.root = root
        root.title("KGI 分鐘K棒下載")
        root.geometry("660x520")
        root.minsize(560, 420)

        form = ttk.Frame(root, padding=12)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        today = datetime.now()
        self.stock_var = tk.StringVar(value="")
        self.minutes_var = tk.StringVar(value="1")
        self.out_var = tk.StringVar(value="")

        def add_row(r, label, widget):
            ttk.Label(form, text=label).grid(row=r, column=0, sticky="w", pady=4)
            widget.grid(row=r, column=1, sticky="we", pady=4, padx=(8, 0))

        # Editable combobox: shows stock codes that already have a
        # {code}_daily.csv / {code}_kbar.csv on disk (per stocks_manifest.json)
        # as dropdown choices, but you can still type any other code freely.
        known_stocks = list_known_stocks()
        self.stock_combo = ttk.Combobox(form, textvariable=self.stock_var, values=known_stocks, width=15)
        self.stock_combo.bind("<<ComboboxSelected>>", self.on_stock_selected)
        add_row(0, "股票代號", self.stock_combo)

        self.start_entry = DateEntry(form, date_pattern="yyyy-mm-dd", width=14, maxdate=today, locale="zh_TW")
        self.start_entry.set_date(today - timedelta(days=30))
        add_row(1, "開始日期", self.start_entry)

        self.end_entry = DateEntry(form, date_pattern="yyyy-mm-dd", width=14, maxdate=today, locale="zh_TW")
        self.end_entry.set_date(today)
        add_row(2, "結束日期", self.end_entry)

        add_row(3, "分鐘週期", ttk.Combobox(
            form, textvariable=self.minutes_var, values=["1", "3", "5", "15", "30", "60"],
            state="readonly", width=10))
        add_row(4, "輸出檔名 (存到 output\\,留空自動命名)", ttk.Entry(form, textvariable=self.out_var))

        self.sim_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="使用模擬環境", variable=self.sim_var).grid(
            row=5, column=1, sticky="w", pady=(0, 4))

        self.run_btn = ttk.Button(form, text="開始下載", command=self.on_run)
        self.run_btn.grid(row=6, column=0, columnspan=2, pady=(8, 0), sticky="we")

        self.log = scrolledtext.ScrolledText(root, height=18, state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.q = queue.Queue()
        self.root.after(100, self.poll_queue)

    def on_stock_selected(self, event=None):
        # Picking a known stock from the dropdown jumps 開始日期 to match the
        # earliest date already in that stock's {code}_kbar.csv. Only touches
        # the field when there's actual kbar data to base it on - a stock
        # with daily-only data, or none yet, leaves the date pickers alone.
        stock = self.stock_var.get().strip()
        if not stock:
            return
        first_date = first_kbar_date(stock)
        if first_date is None:
            return
        self.start_entry.set_date(first_date)

    def append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def poll_queue(self):
        try:
            while True:
                line = self.q.get_nowait()
                if line == "__DONE__":
                    self.run_btn.configure(state="normal")
                else:
                    self.append_log(line)
        except queue.Empty:
            pass
        self.root.after(100, self.poll_queue)

    def on_run(self):
        stock = self.stock_var.get().strip()
        if not stock:
            messagebox.showerror("缺少欄位", "股票代號為必填")
            return

        start_date = self.start_entry.get_date()
        end_date = self.end_entry.get_date()
        if start_date > end_date:
            messagebox.showerror("日期範圍錯誤", "開始日期不能晚於結束日期")
            return
        start = start_date.strftime("%Y-%m-%d")
        end = end_date.strftime("%Y-%m-%d")

        minutes = int(self.minutes_var.get())
        out_name = self.out_var.get().strip() or f"{stock}_kbar.csv"
        simulation = self.sim_var.get()

        # Credential dialogs must run on the main thread (Tkinter isn't
        # thread-safe), so resolve them here before handing off to the
        # background worker, which then only does network I/O + queue.put.
        file_values = load_cred_file(CRED_FILE)
        try:
            person_id = get_credential_gui("KGI_ID", "身分證字號 (login ID)", file_values, self.root)
            person_pwd = get_credential_gui("KGI_PWD", "電子交易密碼", file_values, self.root, hidden=True)
            account = get_credential_gui("KGI_ACCOUNT", "下單帳號", file_values, self.root)
        except RuntimeError as e:
            messagebox.showerror("登入取消", str(e))
            return

        self.run_btn.configure(state="disabled")
        self.append_log(f"\n=== 開始下載 {stock} {start} ~ {end},{minutes} 分K ===\n")
        threading.Thread(
            target=self.worker,
            args=(stock, start, end, minutes, out_name, simulation, person_id, person_pwd, account),
            daemon=True,
        ).start()

    def worker(self, stock, start, end, minutes, out_name, simulation, person_id, person_pwd, account):
        writer = QueueWriter(self.q)
        try:
            with redirect_stdout(writer), redirect_stderr(writer):
                print("Logging in to KGI SUPER PY...", file=sys.stderr)
                api = kgi.login(person_id, person_pwd, simulation)
                api.set_Account(account)

                start_ymd = to_yyyymmdd(start)
                end_ymd = to_yyyymmdd(end)
                out_path = resolve_out_path(out_name)
                skip_dates = existing_kbar_dates(out_path)
                df = fetch_chunks(api, stock, start_ymd, end_ymd, minutes=minutes, skip_dates=skip_dates)

                df = merge_kbar_csv(out_path, df)
                df.to_csv(out_path, index=False, encoding="utf-8-sig")
                update_stocks_manifest(out_name)

                print(f"Fetching daily K for {stock}...", file=sys.stderr)
                daily_df = fetch_daily(api, stock)
                daily_name = f"{stock}_daily.csv"
                daily_path = resolve_out_path(daily_name)
                daily_df.to_csv(daily_path, index=False, encoding="utf-8-sig")
                update_stocks_manifest(daily_name)

            msg = (
                f"\n=== 完成:分鐘K共 {len(df)} 筆,已匯出至 {out_path} ==="
                f"\n=== 日K共 {len(daily_df)} 筆,已匯出至 {daily_path} ==="
            )
            self.q.put(msg + "\n")
        except Exception as e:
            self.q.put(f"\n!!! 發生錯誤: {e}\n")
        finally:
            self.q.put("__DONE__")


def launch_gui():
    root = tk.Tk()
    App(root)
    root.mainloop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

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

    out_path = resolve_out_path(args.out) if args.out else None
    skip_dates = existing_kbar_dates(out_path) if out_path else set()

    print(f"Fetching {args.minutes}-min kbars for {args.stock} from {start_ymd} to {end_ymd}...", file=sys.stderr)
    df = fetch_chunks(api, args.stock, start_ymd, end_ymd, minutes=args.minutes, skip_dates=skip_dates)

    print(df)

    if out_path:
        df = merge_kbar_csv(out_path, df)
        df.to_csv(out_path, index=False, encoding="utf-8-sig")
        print(f"Exported to {out_path}", file=sys.stderr)
        update_stocks_manifest(args.out)

    print(f"Fetching daily K for {args.stock}...", file=sys.stderr)
    daily_df = fetch_daily(api, args.stock)
    daily_name = f"{args.stock}_daily.csv"
    daily_path = resolve_out_path(daily_name)
    daily_df.to_csv(daily_path, index=False, encoding="utf-8-sig")
    print(f"Exported to {daily_path}", file=sys.stderr)
    update_stocks_manifest(daily_name)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        launch_gui()
    else:
        main()
