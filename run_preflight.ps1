$py = '.\.venv\Scripts\python.exe'
$cfg = '.\config.demo.local.yaml'
# No launcher defaults: use only hull.enabled and the exact private profile settings.
Write-Host 'Using enabled symbols, lots, rates and saved per-ticket USD bases from the private config.'


Write-Host "--- VALIDATE CONFIG ---"
& $py -m bot.cli validate-config --config $cfg
if ($LASTEXITCODE -ne 0) { throw 'Invalid config.' }

Write-Host "--- HULL PREFLIGHT ---"
& $py -m bot.cli hull-preflight --config $cfg --paired-confirm
if ($LASTEXITCODE -ne 0) { throw 'Preflight failed.' }
