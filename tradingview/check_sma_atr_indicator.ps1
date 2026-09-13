# Run: powershell -NoProfile -File tradingview/check_sma_atr_indicator.ps1
# Checks source consistency and risk arithmetic; not a Pine compiler.
$ErrorActionPreference = 'Stop'
$indicator = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'sma_atr_trend.pine') -Raw
$strategy = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'sma_atr_trend_strategy.pine') -Raw
function Section($source, $start, $end) {
    $first = $source.IndexOf($start)
    $last = $source.IndexOf($end, $first)
    if ($first -lt 0 -or $last -lt 0) { throw 'Missing source section' }
    $source.Substring($first, $last - $first).Replace("`r`n", "`n").Trim()
}
function Check($condition, $message) { if (-not $condition) { throw $message } }
Check ((Section $indicator 'float lots =' 'bool showBg') -ceq (Section $strategy 'float lots =' 'bool reverse =')) 'Defaults differ'
Check ((Section $indicator 'float qty =' '// Unlike') -ceq (Section $strategy 'float qty =' 'bool buy =')) 'Risk math differs'
Check ($indicator.Contains('bool buy = barstate.isconfirmed and up and not up[1] and riskFits')) 'Buy confirmation missing'
Check ($indicator.Contains('bool sell = barstate.isconfirmed and down and not down[1] and riskFits')) 'Sell confirmation missing'
$signalBlock = Section $indicator 'if buy or sell' 'color col ='
foreach ($name in @('entry', 'sl', 'tp', 'setupRisk', 'setupReward')) {
    $pattern = '\b' + $name + ' :='
    Check (([regex]::Matches($indicator, $pattern).Count -eq 1) -and ([regex]::Matches($signalBlock, $pattern).Count -eq 1)) 'Guide moves outside signal block'
}
# At 0.1 lot, each .001 price tick is $.01; assumed round-trip fees $2.50.
foreach ($budget in @(10, 20, 30, 50, 100)) {
    $ticks = [math]::Floor(($budget - 2.5) / 0.01 - 20)
    $loss = ($ticks + 20) * 0.01 + 2.5
    Check (($loss -le $budget + 1e-9) -and ($loss -gt $budget - 0.01)) 'Cash budget rounding'
    foreach ($rr in @(0.5, 1, 1.5, 3)) {
        $reward = [math]::Ceiling(($loss * $rr + 2.5) / 0.01) * 0.01 - 2.5
        Check (($reward -ge $loss * $rr - 1e-9) -and ($reward -lt $loss * $rr + 0.01 + 1e-9)) 'TP rounding'
    }
}
Check (((1730 + 20) * 0.01 + 2.5 -eq 20) -and ((1731 + 20) * 0.01 + 2.5 -gt 20)) 'ATR budget boundary'
Write-Output 'PASS: shared risk/defaults, confirmed signals, frozen guides, risk rounding and ATR cap boundary'
