$py = '.\.venv\Scripts\python.exe'
$cfg = '.\config.demo.local.yaml'
$profiles = @(
  'XAUUSD:0.10:10:slow'
  'EURUSD:1:10:slow'
  'AUDUSD:1:10:slow'
  'GBPUSD:1:10:slow'
  'GBPJPY:1:10:slow'
  'USDJPY:1.3:10:slow'
  'US30:1:10:slow'
  'US500:1:10:slow'
  'DE30:1:10:slow'
  'JP225:3:10:slow'
) | ForEach-Object { '--profile'; $_ }

Write-Host "--- VALIDATE CONFIG ---"
& $py -m bot.cli validate-config --config $cfg
if ($LASTEXITCODE -ne 0) { throw 'Invalid config.' }

Write-Host "--- HULL PREFLIGHT ---"
& $py -m bot.cli hull-preflight --config $cfg @profiles
if ($LASTEXITCODE -ne 0) { throw 'Preflight failed.' }
