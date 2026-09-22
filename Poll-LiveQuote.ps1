<#
.SYNOPSIS
    Continuously polls TWSE's free MIS real-time quote endpoint for one stock
    and writes the latest snapshot to a JSON file, so a webpage (which can't
    call the quote API directly - no CORS) can poll that file instead.

.DESCRIPTION
    Run this in a terminal and leave it running while you have the live-quote
    HTML page open. Stop with Ctrl+C.

.PARAMETER StockNo
    Stock code, e.g. "6547".

.PARAMETER IntervalSeconds
    How often to refetch. TWSE's MIS endpoint is rate-limited to ~3 requests
    per 5 seconds, so don't go below ~2s.

.PARAMETER OutFile
    Where to write the JSON snapshot (relative paths resolve from wherever
    you run this script).

.EXAMPLE
    .\Poll-LiveQuote.ps1 -StockNo 6547 -OutFile output\6547_live.json
#>
[CmdletBinding()]
param(
    [string]$StockNo = "6547",
    [int]$IntervalSeconds = 3,
    [string]$OutFile = "output\6547_live.json"
)

$ProgressPreference = 'SilentlyContinue'
$headers = @{ "User-Agent" = "Mozilla/5.0" }

$outDir = Split-Path $OutFile -Parent
if ($outDir -and -not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

Write-Host "輪詢 $StockNo,每 $IntervalSeconds 秒更新一次 -> $OutFile (Ctrl+C 停止)" -ForegroundColor Cyan

while ($true) {
    $exChList = "tse_$StockNo.tw|otc_$StockNo.tw"
    $uri = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=$([uri]::EscapeDataString($exChList))&json=1&delay=0"

    try {
        $r = Invoke-RestMethod -Uri $uri -Headers $headers -TimeoutSec 10
        $q = $r.msgArray | Where-Object { $_.c } | Select-Object -First 1

        if ($q) {
            $price     = if ($q.z -and $q.z -ne '-') { [decimal]$q.z } else { $null }
            $prevClose = if ($q.y -and $q.y -ne '-') { [decimal]$q.y } else { $null }
            $change    = if ($null -ne $price -and $null -ne $prevClose) { $price - $prevClose } else { $null }
            $changePct = if ($null -ne $change -and $prevClose -ne 0) { [math]::Round(($change / $prevClose) * 100, 2) } else { $null }

            $obj = [PSCustomObject]@{
                stockNo    = $q.c
                name       = $q.n
                market     = if ($q.ex -eq 'tse') { 'TWSE' } else { 'TPEx' }
                price      = $price
                change     = $change
                changePct  = $changePct
                prevClose  = $prevClose
                open       = if ($q.o -and $q.o -ne '-') { [decimal]$q.o } else { $null }
                high       = if ($q.h -and $q.h -ne '-') { [decimal]$q.h } else { $null }
                low        = if ($q.l -and $q.l -ne '-') { [decimal]$q.l } else { $null }
                volumeLots = if ($q.v -and $q.v -ne '-') { [decimal]$q.v } else { $null }
                time       = $q.t
                date       = $q.d
                updatedAt  = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ss")
            }

            $obj | ConvertTo-Json | Set-Content -Path $OutFile -Encoding utf8
            Write-Host "$(Get-Date -Format 'HH:mm:ss')  $($obj.name) $($obj.price)  $($obj.changePct)%"
        } else {
            Write-Warning "查無 $StockNo 的報價(股票代碼是否正確?)"
        }
    } catch {
        Write-Warning "輪詢失敗: $_"
    }

    Start-Sleep -Seconds $IntervalSeconds
}
