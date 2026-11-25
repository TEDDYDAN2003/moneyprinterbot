$chartProfDir = "C:\Users\hp\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\MQL5\Profiles\Charts"
Get-ChildItem $chartProfDir | ForEach-Object {
    Write-Host "=== Profile: $($_.Name) ==="
    Get-ChildItem $_.FullName -Filter "*.chr" | ForEach-Object {
        $sym = (Get-Content $_.FullName | Select-String "^symbol=" | Select-Object -First 1)
        $per = (Get-Content $_.FullName | Select-String "^period=" | Select-Object -First 1)
        $inds = (Get-Content $_.FullName | Select-String "^<indicator>" -Context 0, 5)
        Write-Host "   $($_.Name): $sym, $per"
        if ($inds) {
            Write-Host "      Indicators found on this chart!"
            $inds | Select-Object -First 5 | ForEach-Object { Write-Host "         $_" }
        }
    }
}
