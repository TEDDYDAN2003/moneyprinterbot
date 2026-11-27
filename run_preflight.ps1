$py = '.\.venv\Scripts\python.exe'
$cfg = '.\config.demo.local.yaml'
# Only the read-only launcher can fall back to the shipped paper template.
if (-not (Test-Path -LiteralPath $cfg -PathType Leaf)) { $cfg = '.\config.yaml' }
Write-Host "Read-only preflight using $cfg; no symbol or target overrides."

Write-Host "--- VALIDATE CONFIG ---"
& $py -m bot.cli validate-config --config $cfg
if ($LASTEXITCODE -ne 0) { throw 'Invalid config.' }

Write-Host "--- HULL PREFLIGHT ---"
& $py -m bot.cli hull-preflight --config $cfg --paired-confirm
if ($LASTEXITCODE -ne 0) { throw 'Preflight failed.' }
