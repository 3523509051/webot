# Re-encode all .ps1 files in this directory as UTF-8 **with BOM**.
#
# Why: Windows PowerShell 5.1 reads a BOM-less .ps1 using the system ANSI
# code page (GBK on Chinese Windows), so any Chinese text in the script
# turns into mojibake. Most editors save as "UTF-8 without BOM" by default,
# so run this after editing any .ps1 here:
#
#     powershell -NoProfile -ExecutionPolicy Bypass -File scripts\fix-bom.ps1
#
# NOTE: this script is intentionally ASCII-only so it works with or without BOM.

$dir = $PSScriptRoot
Get-ChildItem -Path $dir -Filter '*.ps1' |
    Where-Object { $_.Name -ne 'fix-bom.ps1' } |
    ForEach-Object {
        $bytes = [System.IO.File]::ReadAllBytes($_.FullName)
        $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 239 -and $bytes[1] -eq 187 -and $bytes[2] -eq 191)
        $text = Get-Content -Raw -Encoding UTF8 $_.FullName
        Set-Content -Path $_.FullName -Value $text -Encoding UTF8 -NoNewline
        $state = if ($hasBom) { 'already had BOM (rewritten)' } else { 'BOM added' }
        Write-Output ("  {0,-26} {1}" -f $_.Name, $state)
    }
Write-Output 'done.'
