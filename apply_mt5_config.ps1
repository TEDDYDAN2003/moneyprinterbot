$iniPath = "C:\Users\hp\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\config\common.ini"
$exe = "C:\Program Files\MetaTrader 5\terminal64.exe"

# 1. Stop MT5 process gracefully
$p = Get-Process terminal64 -ErrorAction SilentlyContinue
if ($p) {
    Write-Host "Stopping MT5 PID $($p.Id)..."
    Stop-Process -Id $p.Id -Force
    Start-Sleep -Seconds 2
}

# 2. Update common.ini
$ini = Get-Content $iniPath -Raw
$ini = $ini -replace "MaxBars=\d+", "MaxBars=1000"
$ini = $ini -replace "PreloadCharts=\d+", "PreloadCharts=0"
Set-Content -Path $iniPath -Value $ini -NoNewline
Write-Host "Updated common.ini: MaxBars=1000, PreloadCharts=0"

# 3. Restart MT5
Write-Host "Starting MT5 from $exe..."
Start-Process $exe
Start-Sleep -Seconds 6

# 4. Verify MT5 initialized and connected
& ".\.venv\Scripts\python.exe" -c "
import MetaTrader5 as mt5
if mt5.initialize():
    print('MT5 Connected:', mt5.terminal_info().connected)
    pos = mt5.positions_get()
    print('Open positions:', len(pos) if pos else 0)
    mt5.shutdown()
else:
    print('Init failed:', mt5.last_error())
"
