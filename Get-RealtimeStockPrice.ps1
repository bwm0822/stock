<#
.SYNOPSIS
    Fetch real-time (delayed ~5s) quote(s) for one or more Taiwan-listed stocks.

.DESCRIPTION
    Queries the official TWSE MIS (Market Information System) quote endpoint,
    which covers both TWSE (listed) and TPEx (OTC) stocks in a single batched
    request, so no market auto-detection probe is needed.

    Data source: https://mis.twse.com.tw/stock/api/getStockInfo.jsp

    Note: TWSE/TPEx regular trading hours are 09:00-13:30 on trading days.
    Outside that window this returns the last available quote (e.g. the
    closing auction print), not a live-updating price.

.PARAMETER StockNo
    One or more stock codes, e.g. "2330", "6547". Accepts an array for a
    batched multi-stock query in a single request.

.PARAMETER PassThru
    Outputs plain objects instead of the pretty Format-Table view, so the
    result can be piped into Sort-Object/Where-Object/etc.

.EXAMPLE
    .\Get-RealtimeStockPrice.ps1 -StockNo 2330

.EXAMPLE
    .\Get-RealtimeStockPrice.ps1 -StockNo 2330,6547,2454
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$StockNo,

    [switch]$PassThru
)

$ProgressPreference = 'SilentlyContinue'
$headers = @{ "User-Agent" = "Mozilla/5.0" }

function ConvertTo-NullableDecimal {
    param([string]$Value)
    if (-not $Value -or $Value -eq '-') { return $null }
    return [decimal]$Value
}

$exChList = ($StockNo | ForEach-Object { "tse_$_.tw|otc_$_.tw" }) -join "|"
$uri = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=$([uri]::EscapeDataString($exChList))&json=1&delay=0"

try {
    $r = Invoke-RestMethod -Uri $uri -Headers $headers -TimeoutSec 20
} catch {
    throw "Failed to reach TWSE MIS quote API: $_"
}

if ($r.rtcode -ne "0000" -or -not $r.msgArray) {
    throw "TWSE MIS API returned no data (rtcode=$($r.rtcode))."
}

$quotes = $r.msgArray | Where-Object { $_.c }

$foundCodes = $quotes | ForEach-Object { $_.c }
$missing = $StockNo | Where-Object { $_ -notin $foundCodes }
foreach ($m in $missing) {
    Write-Warning "No quote found for stock $m (checked both TWSE and TPEx). Check the stock code."
}

if (-not $quotes) {
    Write-Warning "No matching stocks found."
    return
}

$result = foreach ($q in $quotes) {
    $price = ConvertTo-NullableDecimal $q.z
    $prevClose = ConvertTo-NullableDecimal $q.y
    $change = if ($null -ne $price -and $null -ne $prevClose) { $price - $prevClose } else { $null }
    $changePct = if ($null -ne $change -and $prevClose -ne 0) { ($change / $prevClose) * 100 } else { $null }

    [PSCustomObject]@{
        StockNo    = $q.c
        Name       = $q.n
        Market     = if ($q.ex -eq 'tse') { 'TWSE' } else { 'TPEx' }
        Date       = $q.d
        Time       = $q.t
        Price      = $price
        Change     = $change
        ChangePct  = $changePct
        PrevClose  = $prevClose
        Open       = ConvertTo-NullableDecimal $q.o
        High       = ConvertTo-NullableDecimal $q.h
        Low        = ConvertTo-NullableDecimal $q.l
        VolumeLots = ConvertTo-NullableDecimal $q.v
    }
}

$outputProps = @(
    'StockNo', 'Name', 'Market',
    @{N='Time';E={"$($_.Date.Substring(0,4))-$($_.Date.Substring(4,2))-$($_.Date.Substring(6,2)) $($_.Time)"}},
    @{N='Price';E={ if ($null -eq $_.Price) { 'N/A' } else { $_.Price } }},
    @{N='ChangePct';E={ if ($null -eq $_.ChangePct) { 'N/A' } else { "{0:+0.00;-0.00}%" -f $_.ChangePct } }},
    'PrevClose', 'Open', 'High', 'Low', 'VolumeLots'
)

if ($PassThru) {
    $result | Select-Object $outputProps
} else {
    $result | Select-Object $outputProps | Format-Table -AutoSize
}
