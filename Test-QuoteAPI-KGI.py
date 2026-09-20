"""
Test whether the quoteapi.kgi.com.tw real-time quote channel is authorized
for this account, separate from the tradeapimd historical-data channel that
returned 403 Forbidden.

This does NOT test historical data. It subscribes to live tick/kbar updates
for one stock and prints whatever the server sends back (including any
rejection/event messages) for a short window, then unsubscribes and exits.

On a non-trading day/time, no price ticks will arrive (market closed), but a
permission rejection (if any) should still show up as an event message.

Credentials are resolved in this order: C:\Personal\KGI\kgi.txt (KEY=VALUE
lines, see Get-MinuteKbars-KGI.py for the format) -> environment variables
(KGI_ID / KGI_PWD / KGI_ACCOUNT) -> interactive prompt.

Usage:
    python Test-QuoteAPI-KGI.py --stock 2330 --seconds 15
    python Test-QuoteAPI-KGI.py --stock 2330 --seconds 15 --out-prefix 2330

--out-prefix saves whatever ticks/kbars/events were received during the
listening window to output\<prefix>_ticks.csv / _kbar.csv / _events.csv
(only files with at least one row are written).
"""
import argparse
import dataclasses
import getpass
import os
import sys
import time

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


def to_row(obj):
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    # Tick_Stock_v1 / KBar_Stock_v0 store fields as @property-wrapped private
    # attributes, so vars()/__dict__ can come back empty. Read every public
    # (non-underscore, non-callable) attribute instead - that goes through
    # the property getters regardless of how the value is actually stored.
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


def save_rows(rows, filename):
    if not rows:
        return
    path = resolve_out_path(filename)
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    print(f"Saved {len(rows)} row(s) to {path}", file=sys.stderr)


def make_on_event(collected):
    def on_event(event):
        print(f"[EVENT] api_id={event.api_id} code={event.event_code} respond={event.respond_code} msg={event.event_msg} info={event.info}")
        collected.append({
            "api_id": event.api_id,
            "event_code": str(event.event_code),
            "respond_code": str(event.respond_code),
            "event_msg": event.event_msg,
            "info": event.info,
        })
    return on_event


def make_on_tick(collected):
    def on_tick(tick, *args, **kwargs):
        print(f"[TICK] {tick}")
        collected.append(to_row(tick))
    return on_tick


def make_on_kbar(collected):
    def on_kbar(kbar, *args, **kwargs):
        print(f"[KBAR] {kbar}")
        collected.append(to_row(kbar))
    return on_kbar


def main():
    parser = argparse.ArgumentParser(description="Test KGI quoteapi live subscription")
    parser.add_argument("--stock", default="2330", help="Stock code, e.g. 2330")
    parser.add_argument("--seconds", type=int, default=15, help="How long to listen before exiting")
    parser.add_argument("--simulation", action="store_true", help="Use simulation environment instead of production")
    parser.add_argument("--out-prefix", help="Save received ticks/kbars/events to <prefix>_ticks.csv etc.")
    args = parser.parse_args()

    file_values = load_cred_file(CRED_FILE)
    person_id = get_credential("KGI_ID", "身分證字號 (login ID)", file_values)
    person_pwd = get_credential("KGI_PWD", "電子交易密碼", file_values, hidden=True)
    account = get_credential("KGI_ACCOUNT", "下單帳號", file_values)

    events, ticks, kbars = [], [], []

    print("Logging in to KGI SUPER PY...", file=sys.stderr)
    api = kgi.login(person_id, person_pwd, args.simulation)
    api.set_Account(account)

    print(f"Subscribing to tick + kbar for {args.stock} on quoteapi...", file=sys.stderr)
    api.Quote.set_cb_event(make_on_event(events))
    api.Quote.set_cb_tick(make_on_tick(ticks))
    api.Quote.set_cb_kbar(make_on_kbar(kbars))
    api.Quote.subscribe_tick(args.stock)
    api.Quote.subscribe_kbar(args.stock, minute=1)

    print(f"Listening for {args.seconds} seconds (market closed = no price ticks expected, "
          f"but a permission rejection would show as an [EVENT] line)...", file=sys.stderr)
    time.sleep(args.seconds)

    print("Current subscriptions:", api.Quote.get_subscriptions())
    api.Quote.unsubscribe_all()

    if args.out_prefix:
        save_rows(ticks, f"{args.out_prefix}_ticks.csv")
        save_rows(kbars, f"{args.out_prefix}_kbar.csv")
        save_rows(events, f"{args.out_prefix}_events.csv")


if __name__ == "__main__":
    main()
