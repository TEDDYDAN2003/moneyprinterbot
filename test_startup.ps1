$exe = "C:\Program Files\MetaTrader 5\terminal64.exe"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
Start-Process $exe
Start-Sleep -Seconds 3
$sw.Stop()
Write-Host "MT5 process launched in $($sw.ElapsedMilliseconds) ms."

& ".\.venv\Scripts\python.exe" -c "
import MetaTrader5 as mt5
import time
t0 = time.time()
if mt5.initialize():
    t1 = time.time()
    print('MT5 Python API connected in: ' + str(round(t1 - t0, 2)) + 's')
    print('Terminal connected:', mt5.terminal_info().connected)
    print('Ping (ms):', mt5.terminal_info().ping_last)
    pos = mt5.positions_get()
    print('Open positions:', len(pos) if pos else 0)
    mt5.shutdown()
else:
    print('Init failed:', mt5.last_error())
"
Get-Process terminal* | Select-Object Id, ProcessName, @{Name="RAM_MB"; Expression={[math]::Round($_.WorkingSet64 / 1MB, 2)}}
