[CmdletBinding()]
param([switch]$Observe)

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

if ($Observe) {
  & $py -m bot.cli trade-all --config $cfg --m1-confirm @profiles
} else {
  & $py -m bot.cli trade-all --config $cfg --m1-confirm --arm-demo @profiles
}
exit $LASTEXITCODE
