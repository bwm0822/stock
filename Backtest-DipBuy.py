"""
Backtest a simple dip-buy strategy on minute-level Kbar data:
    Buy once a day's price first drops to (that day's open * (1 - buy_pct/100)).
    Sell once it then rises to (buy day's open * (1 + sell_pct/100)).
    Only one position at a time - while holding, no new buy signals are
    taken on other days until the current position sells.

Two exit modes (--mode):
    forced_exit (day-trading): must close same day. If the sell target is
        never hit, force-exit at that day's last close.
    hold_until_target (default): no same-day requirement. Keep holding
        across as many days as it takes until the sell target is hit.

Capital compounds trade to trade. Position size is whole board lots (1 lot
= 1000 shares) using all available capital. Includes standard brokerage fee
(0.1425% each way). Securities transaction tax on the sell side is 0.15%
(the day-trade rate) only when buy and sell land on the same date, else the
normal 0.3% rate.

Usage:
    python Backtest-DipBuy.py --minute-csv output/6547_minute.csv --capital 1200000 --buy-pct 1 --sell-pct 1
    python Backtest-DipBuy.py --mode forced_exit --minute-csv output/6547_minute.csv
"""
import argparse

import pandas as pd

FEE_RATE = 0.001425
DAYTRADE_TAX_RATE = 0.0015
NORMAL_TAX_RATE = 0.003
LOT_SIZE = 1000


def load_minute_data(path):
    df = pd.read_csv(path)
    df = df.sort_values(["日期", "hhmm"]).reset_index(drop=True)
    return df


def settle_trade(capital, buy_price, sell_price, buy_date, sell_date):
    lots = int(capital // (buy_price * LOT_SIZE))
    if lots <= 0:
        return None
    shares = lots * LOT_SIZE
    tax_rate = DAYTRADE_TAX_RATE if buy_date == sell_date else NORMAL_TAX_RATE

    cost = shares * buy_price
    buy_fee = cost * FEE_RATE
    proceeds = shares * sell_price
    sell_fee = proceeds * FEE_RATE
    sell_tax = proceeds * tax_rate
    net = proceeds - sell_fee - sell_tax - cost - buy_fee
    return lots, net


def backtest(df, capital, buy_pct, sell_pct, mode, stop_loss_pct=None,
             confirm_bars=None, confirm_recovery_pct=None, max_gap_pct=None, confirm_retest=False):
    capital_start = capital
    trades = []
    holding = False
    buy_price = buy_date = buy_hhmm = sell_trigger = stop_trigger = None
    confirm_enabled = bool(confirm_bars or confirm_recovery_pct or confirm_retest)
    prev_close = None

    for date, day in df.groupby("日期"):
        day = day.reset_index(drop=True)
        day_open = day.loc[0, "開盤價"]
        buy_trigger = day_open * (1 - buy_pct / 100)
        entered_today = False
        exited_today = False

        # Gap filter: skip watching for a buy signal at all today if the
        # open gapped up too far from yesterday's close - a big morning gap
        # that then pulls back -buy_pct% is often "gap and crap" (chasing an
        # overextended jump), not a normal dip. Only applies when NOT already
        # holding - a stop-loss/sell-target check on an existing position
        # still runs regardless of today's gap.
        gap_pct = (day_open - prev_close) / prev_close * 100 if prev_close else None
        skip_today = (
            not holding and max_gap_pct is not None
            and gap_pct is not None and gap_pct > max_gap_pct
        )
        prev_close = day.iloc[-1]["收盤價"]

        if skip_today:
            if not exited_today:
                trades.append({"date": date, "traded": False, "note": f"開盤跳空 +{gap_pct:.2f}%,超過門檻不追,今天不進場"})
            continue

        # Reversal-confirmation state: once price touches buy_trigger, don't
        # buy immediately. Three alternative confirmations (any one satisfies it):
        #   confirm_bars: this many consecutive rising closes in a row (any
        #     single drop resets the streak to 0 - brittle to noise).
        #   confirm_recovery_pct: price has recovered this %% off the lowest
        #     point seen since the trigger (tolerates noisy pullbacks within
        #     a genuine bounce, unlike the streak-based check).
        #   confirm_retest: price dipped below buy_trigger, then climbed back
        #     up to buy_trigger itself (reclaimed that exact level) - no
        #     percentage to tune, the retest level is the trigger price.
        # Resets every day - a trigger from a prior day doesn't carry over.
        awaiting_confirm = False
        confirm_streak = 0
        last_watch_price = None
        watch_low = None

        for _, row in day.iterrows():
            price = row["收盤價"]

            if not holding and not awaiting_confirm and price <= buy_trigger:
                if confirm_enabled:
                    awaiting_confirm = True
                    confirm_streak = 0
                    last_watch_price = price
                    watch_low = price
                    continue
                holding = True
                entered_today = True
                buy_price = price
                buy_date = date
                buy_hhmm = row["hhmm"]
                sell_trigger = day_open * (1 + sell_pct / 100)
                stop_trigger = buy_price * (1 - stop_loss_pct / 100) if stop_loss_pct else None
                continue

            if awaiting_confirm and not holding:
                watch_low = min(watch_low, price)
                confirm_streak = confirm_streak + 1 if price > last_watch_price else 0
                last_watch_price = price

                bars_ok = confirm_bars and confirm_streak >= confirm_bars
                recovery_ok = confirm_recovery_pct and price >= watch_low * (1 + confirm_recovery_pct / 100)
                retest_ok = confirm_retest and price >= buy_trigger

                if bars_ok or recovery_ok or retest_ok:
                    awaiting_confirm = False
                    holding = True
                    entered_today = True
                    buy_price = price
                    buy_date = date
                    buy_hhmm = row["hhmm"]
                    sell_trigger = day_open * (1 + sell_pct / 100)
                    stop_trigger = buy_price * (1 - stop_loss_pct / 100) if stop_loss_pct else None
                continue

            if holding and stop_trigger is not None and price <= stop_trigger:
                result = settle_trade(capital, buy_price, price, buy_date, date)
                holding = False
                exited_today = True
                if result is None:
                    trades.append({"date": buy_date, "traded": False, "note": "資金不足買1張"})
                    break
                lots, net = result
                capital += net
                trades.append({
                    "date": buy_date, "traded": True, "lots": lots,
                    "buy_time": buy_hhmm, "buy_price": buy_price,
                    "sell_date": date, "sell_time": row["hhmm"], "sell_price": price,
                    "reason": "stop_loss",
                    "net_profit": round(net), "capital_after": round(capital),
                })
                break

            if holding and price >= sell_trigger:
                result = settle_trade(capital, buy_price, price, buy_date, date)
                holding = False
                exited_today = True
                if result is None:
                    trades.append({"date": buy_date, "traded": False, "note": "資金不足買1張"})
                    break
                lots, net = result
                capital += net
                trades.append({
                    "date": buy_date, "traded": True, "lots": lots,
                    "buy_time": buy_hhmm, "buy_price": buy_price,
                    "sell_date": date, "sell_time": row["hhmm"], "sell_price": price,
                    "reason": "target",
                    "net_profit": round(net), "capital_after": round(capital),
                })
                break

        if mode == "forced_exit" and holding and entered_today:
            # Day-trading can't carry overnight: force-close at this day's last print.
            price = day.iloc[-1]["收盤價"]
            result = settle_trade(capital, buy_price, price, buy_date, date)
            holding = False
            exited_today = True
            if result is None:
                trades.append({"date": buy_date, "traded": False, "note": "資金不足買1張"})
            else:
                lots, net = result
                capital += net
                trades.append({
                    "date": buy_date, "traded": True, "lots": lots,
                    "buy_time": buy_hhmm, "buy_price": buy_price,
                    "sell_date": date, "sell_time": day.iloc[-1]["hhmm"], "sell_price": price,
                    "reason": "forced_close",
                    "net_profit": round(net), "capital_after": round(capital),
                })

        if not entered_today and not exited_today and not holding:
            trades.append({"date": date, "traded": False})

    if holding:
        trades.append({"date": buy_date, "traded": False, "note": "持股到資料結尾仍未賣出(未實現)"})

    return trades, capital, capital_start


def main():
    parser = argparse.ArgumentParser(description="Backtest a daily -X% buy / +Y% sell dip-buy strategy")
    parser.add_argument("--minute-csv", default="output/6547_minute.csv")
    parser.add_argument("--capital", type=float, default=1_200_000)
    parser.add_argument("--buy-pct", type=float, default=1.0)
    parser.add_argument("--sell-pct", type=float, default=1.0)
    parser.add_argument("--mode", choices=["hold_until_target", "forced_exit"], default="hold_until_target",
                         help="hold_until_target (default): no same-day exit, hold until +sell_pct%% is hit, "
                              "even across multiple days. forced_exit: must close same day (day-trading).")
    parser.add_argument("--stop-loss-pct", type=float, default=None,
                         help="Exit immediately (any time, any day) if price falls this %% below the buy price. "
                              "Default: no stop-loss, hold until the sell target regardless of drawdown.")
    parser.add_argument("--confirm-bars", type=int, default=None,
                         help="After price touches the buy trigger, wait for this many consecutive rising "
                              "1-min closes before actually buying (any single drop resets the streak). "
                              "Default: buy immediately on touch, no confirmation.")
    parser.add_argument("--confirm-recovery-pct", type=float, default=None,
                         help="After price touches the buy trigger, wait until price recovers this %% off the "
                              "lowest point seen since the trigger before buying (tolerates noisy pullbacks "
                              "within a bounce, unlike --confirm-bars). Either confirmation satisfies the wait "
                              "if both are given.")
    parser.add_argument("--max-gap-pct", type=float, default=None,
                         help="Skip watching for a buy signal entirely on a day whose open gapped up more than "
                              "this %% from the previous day's close (avoids chasing an overextended 'gap and "
                              "crap' day). Doesn't affect an already-open position's sell/stop-loss checks.")
    parser.add_argument("--confirm-retest", action="store_true",
                         help="After price dips below the buy trigger, wait for it to climb back up to the "
                              "trigger price itself (reclaim that level) before buying - no percentage to tune.")
    args = parser.parse_args()

    df = load_minute_data(args.minute_csv)
    trades, final_capital, start_capital = backtest(
        df, args.capital, args.buy_pct, args.sell_pct, args.mode, args.stop_loss_pct,
        args.confirm_bars, args.confirm_recovery_pct, args.max_gap_pct, args.confirm_retest)

    executed = [t for t in trades if t.get("traded")]
    no_trade_days = [t for t in trades if not t.get("traded")]

    stop_desc = f", 停損: -{args.stop_loss_pct}%" if args.stop_loss_pct else ", 停損: 無"
    confirm_bits = []
    if args.confirm_bars:
        confirm_bits.append(f"連{args.confirm_bars}根上漲")
    if args.confirm_recovery_pct:
        confirm_bits.append(f"低點反彈{args.confirm_recovery_pct}%")
    if args.confirm_retest:
        confirm_bits.append("回測觸發價")
    confirm_desc = f", 反彈確認: {' 或 '.join(confirm_bits)}" if confirm_bits else ", 反彈確認: 無(觸價即買)"
    gap_desc = f", 跳空濾網: 開盤漲超過{args.max_gap_pct}%不追" if args.max_gap_pct else ", 跳空濾網: 無"
    print(f"策略: 每日開盤 -{args.buy_pct}% 買進, +{args.sell_pct}% 賣出 | 模式: {args.mode}{stop_desc}{confirm_desc}{gap_desc} | 初始資金: {start_capital:,.0f}\n")

    reason_label = {"target": "達標", "stop_loss": "停損", "forced_close": "強制平倉"}
    print(f"{'買進日期':<12}{'買進時間':<8}{'買進價':>8}  {'賣出日期':<12}{'賣出時間':<8}{'賣出價':>8}{'張數':>6}{'當筆獲利':>10}{'累積獲利':>12}{'跨日':>6}  {'原因'}")
    running = start_capital
    for t in executed:
        held_over = t['date'] != t['sell_date']
        running = t['capital_after']
        cumulative = int(running - start_capital)
        print(f"{t['date']:<12}{t['buy_time']:<8}{t['buy_price']:>8.2f}  {t['sell_date']:<12}{t['sell_time']:<8}{t['sell_price']:>8.2f}"
              f"{t['lots']:>6}{t['net_profit']:>10,}{cumulative:>12,}{'是' if held_over else '':>6}  {reason_label.get(t.get('reason'), '')}")

    total_profit = final_capital - start_capital
    print(f"\n完成交易筆數: {len(executed)}, 未進場/未平倉天數: {len(no_trade_days)}")
    for t in no_trade_days:
        if t.get("note"):
            print(f"  {t['date']}: {t['note']}")
    print(f"總損益: {total_profit:,.0f} 元")
    print(f"期末資金: {final_capital:,.0f} 元")
    print(f"報酬率: {total_profit / start_capital * 100:.2f}%")


if __name__ == "__main__":
    main()
