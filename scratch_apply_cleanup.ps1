$termDir = "C:\Users\hp\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075"
$iniPath = Join-Path $termDir "config\common.ini"

# 1. Stop MT5 if running
$p = Get-Process terminal64 -ErrorAction SilentlyContinue
if ($p) {
    Write-Host "Stopping MT5 PID $($p.Id)..."
    Stop-Process -Id $p.Id -Force
    Start-Sleep -Seconds 2
} else {
    Write-Host "MT5 is not running."
}

# 2. Update common.ini: ProfileLast=Default, NewsEnable=0
if (Test-Path $iniPath) {
    $ini = Get-Content $iniPath -Raw
    $ini = $ini -replace "ProfileLast=.*", "ProfileLast=Default"
    $ini = $ini -replace "NewsEnable=\d+", "NewsEnable=0"
    Set-Content -Path $iniPath -Value $ini -NoNewline
    Write-Host "Updated common.ini (ProfileLast=Default, NewsEnable=0)."
}

# 3. Clear old .hc cache files from history directories
$cacheDirs = Get-ChildItem "$termDir\bases\ExnessKE-MT5Trial10\history" -Directory -Filter "cache" -Recurse -ErrorAction SilentlyContinue
$totalFreed = 0
$fileCount = 0
foreach ($dir in $cacheDirs) {
    $files = Get-ChildItem $dir.FullName -Filter "*.hc" -ErrorAction SilentlyContinue
    foreach ($f in $files) {
        $totalFreed += $f.Length
        $fileCount++
        Remove-Item $f.FullName -Force -ErrorAction SilentlyContinue
    }
}
Write-Host "Cleared $fileCount old .hc cache files ($([math]::Round($totalFreed / 1MB, 2)) MB freed)."

# 4. Check results in common.ini
Get-Content $iniPath | Select-String "ProfileLast|NewsEnable|MaxBars|PreloadCharts"
