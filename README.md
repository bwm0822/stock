# stock

Scripts for pulling Taiwan stock market data (TWSE/TPEx daily OHLC, real-time quotes,
and historical minute/daily Kbars via KGI Securities' `kgisuperpy` API).

## Scripts

- **Get-StockPrice.ps1** — Daily OHLC for a TWSE/TPEx stock via the official free
  TWSE/TPEx JSON APIs (no account needed). Supports filtering for two-way
  intraday volatility days and CSV/XLSX export.
- **Get-RealtimeStockPrice.ps1** — Live/delayed quote snapshot(s) via TWSE's MIS
  endpoint, covers both TWSE and TPEx in one batched request.
- **kbar.py** — Historical minute-level Kbars over an arbitrary
  date range, via KGI's `kgisuperpy` API. Requires a KGI Securities account with
  API access approved. Every run also fetches the full daily-K history for the
  stock (needed for `stocks.html`'s chart) and, when re-fetching a stock you've
  already downloaded, only requests the missing days and merges into the
  existing file. Run with no arguments for a Tkinter GUI (stock/date range/
  minutes fields, login dialogs, live progress log); run with any argument for
  the original CLI, which also supports `--list-tables` to dump the account's
  live Data.get() table catalog.
- **Get-DailyK-KGI.py** — Daily/weekly/monthly Kbars with technical indicators
  (MA, MACD, RSI, KD, Bollinger Bands, ADX) and chip/margin data, via the same
  KGI API. Returns the full available history for a symbol in one call.
- **Test-QuoteAPI-KGI.py** — Sanity-checks the KGI real-time quote push channel
  (`quoteapi.kgi.com.tw`) independently of the historical-data endpoint.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install kgisuperpy pandas
pip install tkcalendar   # only needed for kbar.py's GUI mode
```

The KGI scripts need `kgisuperpy`'s native COM component registered
(`KGICGCAPIATL2x64.dll`, from KGI's official API download) and a KGI Securities
account with API access approved via their broker portal.

## Credentials

The KGI scripts resolve credentials in this order:

1. A `kgi.txt` file (KEY=VALUE lines: `KGI_ID`, `KGI_PWD`, `KGI_ACCOUNT`) at the
   path configured in each script's `CRED_FILE` constant.
2. Environment variables `KGI_ID` / `KGI_PWD` / `KGI_ACCOUNT`.
3. Interactive prompt (password input hidden).

Never commit `kgi.txt` or any file containing real credentials.
