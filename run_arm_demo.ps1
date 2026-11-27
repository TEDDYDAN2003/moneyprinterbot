[CmdletBinding()]
param([switch]$Observe)

$py = '.\.venv\Scripts\python.exe'
$cfg = '.\config.demo.local.yaml'
# No launcher defaults: use only hull.enabled and the exact private profile settings.
Write-Host 'Using enabled symbols, lots, rates and saved per-ticket USD bases from the private config.'


if ($Observe) {
  & $py -m bot.cli trade-all --config $cfg --paired-confirm
} else {
  & $py -m bot.cli trade-all --config $cfg --paired-confirm --arm-demo
}
exit $LASTEXITCODE
