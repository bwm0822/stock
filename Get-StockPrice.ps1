<#
.SYNOPSIS
    Fetch daily OHLC stock price data for a Taiwan-listed stock (TWSE or TPEx).

.DESCRIPTION
    Auto-detects whether a stock trades on TWSE (Taiwan Stock Exchange, "listed")
    or TPEx (Taipei Exchange, "OTC"), then calls the official month-based JSON
    APIs to pull daily open/high/low/close/volume for a date range.

    Data sources:
      - TWSE (listed): https://www.twse.com.tw/exchangeReport/STOCK_DAY
      - TPEx (OTC):     https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock

.PARAMETER StockNo
    Stock code, e.g. "6547" (Medigen Vaccine), "2330" (TSMC).

.PARAMETER StartDate
    Start of the query range. Defaults to one month ago.

.PARAMETER EndDate
    End of the query range. Defaults to today.

.PARAMETER Market
    "TWSE", "TPEx", or "Auto" (default - tries TWSE first, falls back to TPEx).

.PARAMETER OutFile
    Optional path to also export the result as CSV.

.PARAMETER VolatilityPct
    Optional. Symmetric shorthand for -DownPct/-UpPct: only keeps days with
    two-way intraday volatility of at least this percent from the open in
    BOTH directions: Low <= Open*(1-pct/100) AND High >= Open*(1+pct/100).
    Adds LowAmpPct/HighAmpPct/TotalAmpPct columns to the output.

.PARAMETER DownPct
    Optional. Minimum drop from open required, in percent (e.g. 1 = -1%).
    Only keeps days where Low <= Open*(1-DownPct/100). Combine with -UpPct
    for asymmetric two-way filtering; overrides -VolatilityPct's down side.

.PARAMETER UpPct
    Optional. Minimum rise from open required, in percent (e.g. 2 = +2%).
    Only keeps days where High >= Open*(1+UpPct/100). Combine with -DownPct
    for asymmetric two-way filtering; overrides -VolatilityPct's up side.

.EXAMPLE
    .\Get-StockPrice.ps1 -StockNo 6547

.EXAMPLE
    .\Get-StockPrice.ps1 -StockNo 2330 -StartDate 2026-08-01 -EndDate 2026-08-31

.EXAMPLE
    .\Get-StockPrice.ps1 -StockNo 6547 -OutFile .\6547.csv

.EXAMPLE
    .\Get-StockPrice.ps1 -StockNo 6547 -VolatilityPct 1

.EXAMPLE
    .\Get-StockPrice.ps1 -StockNo 6547 -DownPct 1 -UpPct 2

.PARAMETER PassThru
    Outputs plain objects instead of the pretty Format-Table view, so the
    result can be piped into Sort-Object/Where-Object/etc. for further
    analysis (e.g. finding the day with the highest/lowest TotalAmpPct).

.PARAMETER HideAmplitude
    By default, every run adds LowAmpPct/HighAmpPct/TotalAmpPct columns
    (how far Low/High moved from Open, as a percent). Pass -HideAmplitude
    to omit them and show only Date/Open/High/Low/Close/Volume.

.EXAMPLE
    .\Get-StockPrice.ps1 -StockNo 2454 -HideAmplitude
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$StockNo,

    [datetime]$StartDate = (Get-Date).AddMonths(-1),

    [datetime]$EndDate = (Get-Date),

    [switch]$HideAmplitude,

    [ValidateSet("Auto", "TWSE", "TPEx")]
    [string]$Market = "Auto",

    [string]$OutFile,

    [double]$VolatilityPct,

    [double]$DownPct,

    [double]$UpPct,

    [switch]$PassThru
)

if (-not $DownPct) { $DownPct = $VolatilityPct }
if (-not $UpPct) { $UpPct = $VolatilityPct }

$ProgressPreference = 'SilentlyContinue'
$headers = @{ "User-Agent" = "Mozilla/5.0" }

function ConvertFrom-RocDate {
    param([string]$RocDate)
    # Input format "115/09/16" (ROC year/month/day) -> [datetime]
    $parts = $RocDate -split '/'
    if ($parts.Count -ne 3) { return $null }
    $year = [int]$parts[0] + 1911
    return (Get-Date -Year $year -Month ([int]$parts[1]) -Day ([int]$parts[2])).Date
}

function Get-TwseMonth {
    param([string]$StockNo, [datetime]$MonthDate)
    $dateParam = $MonthDate.ToString("yyyyMMdd")
    $uri = "https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date=$dateParam&stockNo=$StockNo"
    try {
        $r = Invoke-RestMethod -Uri $uri -TimeoutSec 20 -Headers $headers
    } catch {
        return @()
    }
    if ($r.stat -ne "OK" -or -not $r.data) { return @() }

    $result = foreach ($row in $r.data) {
        $d = ConvertFrom-RocDate $row[0]
        if (-not $d) { continue }
        [PSCustomObject]@{
            Date   = $d
            Open   = [decimal]($row[3] -replace ',', '')
            High   = [decimal]($row[4] -replace ',', '')
            Low    = [decimal]($row[5] -replace ',', '')
            Close  = [decimal]($row[6] -replace ',', '')
            Volume = [long]($row[1] -replace ',', '')
        }
    }
    return $result
}

function Close-LockedOutputFile {
    # If $Path is currently open in Excel (common after a prior export was
    # opened to view), close that window so this run can overwrite it.
    param([string]$Path)
    if (-not (Test-Path $Path)) { return }
    $locked = $false
    try {
        $stream = [System.IO.File]::Open($Path, 'Open', 'ReadWrite', 'None')
        $stream.Close()
    } catch {
        $locked = $true
    }
    if (-not $locked) { return }

    $baseName = [System.IO.Path]::GetFileName($Path)
    $procs = Get-Process -Name EXCEL -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowTitle -like "*$baseName*" }
    foreach ($p in $procs) {
        Write-Warning "'$baseName' was open in Excel; closing that window (without saving) so it can be overwritten."
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    }
    if ($procs) { Start-Sleep -Milliseconds 700 }
}

function Get-TpexMonth {
    param([string]$StockNo, [datetime]$MonthDate)
    $dateParam = $MonthDate.ToString("yyyy/MM/01")
    $uri = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?code=$StockNo&date=$dateParam"
    try {
        $r = Invoke-RestMethod -Uri $uri -TimeoutSec 20 -Headers $headers
    } catch {
        return @()
    }
    if (-not $r.tables -or -not $r.tables[0].data) { return @() }

    $result = foreach ($row in $r.tables[0].data) {
        $d = ConvertFrom-RocDate $row[0]
        if (-not $d) { continue }
        [PSCustomObject]@{
            Date   = $d
            Open   = [decimal]($row[3] -replace ',', '')
            High   = [decimal]($row[4] -replace ',', '')
            Low    = [decimal]($row[5] -replace ',', '')
            Close  = [decimal]($row[6] -replace ',', '')
            Volume = [long]($row[1] -replace ',', '') * 1000
        }
    }
    return $result
}

function Get-MonthStarts {
    param([datetime]$From, [datetime]$To)
    $cursor = Get-Date -Year $From.Year -Month $From.Month -Day 1
    $last = Get-Date -Year $To.Year -Month $To.Month -Day 1
    $months = @()
    while ($cursor -le $last) {
        $months += $cursor
        $cursor = $cursor.AddMonths(1)
    }
    return $months
}

if ($StartDate -gt $EndDate) {
    throw "StartDate must not be later than EndDate"
}

$months = Get-MonthStarts -From $StartDate -To $EndDate
$resolvedMarket = $Market

$allRows = @()

if ($Market -eq "Auto") {
    # Use the first month to detect the market: try TWSE first, then TPEx.
    $probe = Get-TwseMonth -StockNo $StockNo -MonthDate $months[0]
    if ($probe.Count -gt 0) {
        $resolvedMarket = "TWSE"
        $allRows += $probe
    } else {
        $probe = Get-TpexMonth -StockNo $StockNo -MonthDate $months[0]
        if ($probe.Count -gt 0) {
            $resolvedMarket = "TPEx"
            $allRows += $probe
        } else {
            throw "No data found for stock $StockNo on either TWSE or TPEx. Please check the stock code."
        }
    }
    $remainingMonths = $months | Select-Object -Skip 1
} else {
    $remainingMonths = $months
}

foreach ($m in $remainingMonths) {
    if ($resolvedMarket -eq "TWSE") {
        $allRows += Get-TwseMonth -StockNo $StockNo -MonthDate $m
    } else {
        $allRows += Get-TpexMonth -StockNo $StockNo -MonthDate $m
    }
}

$allRowsSorted = $allRows | Sort-Object Date
for ($i = 0; $i -lt $allRowsSorted.Count; $i++) {
    $prevClose = if ($i -gt 0) { $allRowsSorted[$i - 1].Close } else { $null }
    Add-Member -InputObject $allRowsSorted[$i] -MemberType NoteProperty -Name PrevClose -Value $prevClose -Force
}

$filtered = $allRowsSorted |
    Where-Object { $_.Date -ge $StartDate.Date -and $_.Date -le $EndDate.Date }

if ($filtered.Count -eq 0) {
    Write-Warning "No trading data found in the specified date range."
    return
}

if ($DownPct -or $UpPct) {
    $downFactor = 1 - ($DownPct / 100)
    $upFactor = 1 + ($UpPct / 100)
    $filtered = $filtered | Where-Object {
        $_.Low -le ($_.Open * $downFactor) -and $_.High -ge ($_.Open * $upFactor)
    }
}

Write-Host "StockNo: $StockNo   Market: $resolvedMarket   Range: $($StartDate.ToString('yyyy-MM-dd')) ~ $($EndDate.ToString('yyyy-MM-dd'))" -ForegroundColor Cyan
if ($DownPct -or $UpPct) {
    Write-Host "Filter: Low <= Open*-$DownPct%  AND  High >= Open*+$UpPct%  ($($filtered.Count) matching day(s))" -ForegroundColor Cyan
}

if ($filtered.Count -eq 0) {
    Write-Warning "No days matched the volatility filter."
    return
}

$outputProps = @(
    @{N='Date';E={$_.Date.ToString('yyyy-MM-dd')}}, 'Open', 'High', 'Low', 'Close',
    @{N='ChangePct';E={
        if ($null -eq $_.PrevClose) { 'N/A' }
        else { "{0:+0.00;-0.00}%" -f ((($_.Close - $_.PrevClose)/$_.PrevClose)*100) }
    }},
    'Volume'
)
if (-not $HideAmplitude) {
    # LowAmpPct: how far below Open the Low fell, shown as a negative percent.
    # HighAmpPct: how far above Open the High rose, as a positive percent.
    # TotalAmpPct: full (High-Low) range as a percent of Open = HighAmpPct - LowAmpPct.
    $outputProps += @{N='LowAmpPct';E={"{0:+0.00;-0.00}%" -f ((($_.Low - $_.Open)/$_.Open)*100)}}
    $outputProps += @{N='HighAmpPct';E={"{0:+0.00;-0.00}%" -f ((($_.High - $_.Open)/$_.Open)*100)}}
    $outputProps += @{N='TotalAmpPct';E={"{0:0.00}%" -f ((($_.High - $_.Low)/$_.Open)*100)}}
}

if ($OutFile) {
    $ext = [System.IO.Path]::GetExtension($OutFile).ToLower()
    if ($ext -eq '.xlsx' -or $ext -eq '.xls') {
        if ($ext -eq '.xls') {
            Write-Warning "Legacy .xls is not supported; writing modern .xlsx format instead at the same path."
            $OutFile = [System.IO.Path]::ChangeExtension($OutFile, '.xlsx')
        }
        Close-LockedOutputFile -Path $OutFile
        if (-not (Get-Module -ListAvailable -Name ImportExcel)) {
            throw "The ImportExcel module is required for Excel export. Install it with: Install-Module ImportExcel -Scope CurrentUser"
        }
        Import-Module ImportExcel -ErrorAction Stop
        Add-Type -AssemblyName System.Drawing -ErrorAction SilentlyContinue
        $xlPkg = $filtered | Select-Object $outputProps |
            Export-Excel -Path $OutFile -WorksheetName "$StockNo" -AutoSize -FreezeTopRow -BoldTopRow -ClearSheet -PassThru
        $ws = $xlPkg.Workbook.Worksheets["$StockNo"]

        # TW convention: red = up (漲), green = down (跌). Applies to any
        # column whose values are formatted with a leading +/- sign.
        $signedCols = @('ChangePct', 'LowAmpPct', 'HighAmpPct')
        for ($c = 1; $c -le $ws.Dimension.End.Column; $c++) {
            if ($signedCols -contains $ws.Cells[1, $c].Text) {
                for ($r = 2; $r -le $ws.Dimension.End.Row; $r++) {
                    $val = $ws.Cells[$r, $c].Text
                    if ($val.StartsWith('+')) {
                        $ws.Cells[$r, $c].Style.Font.Color.SetColor([System.Drawing.Color]::Red)
                    } elseif ($val.StartsWith('-')) {
                        $ws.Cells[$r, $c].Style.Font.Color.SetColor([System.Drawing.Color]::Green)
                    }
                }
            }
        }

        Close-ExcelPackage $xlPkg
    } else {
        Close-LockedOutputFile -Path $OutFile
        $filtered | Select-Object $outputProps | Export-Csv -Path $OutFile -NoTypeInformation -Encoding UTF8
    }
    Write-Host "Exported to $OutFile" -ForegroundColor Green
}

if ($PassThru) {
    $filtered | Select-Object $outputProps
} else {
    $filtered | Select-Object $outputProps | Format-Table -AutoSize
}
