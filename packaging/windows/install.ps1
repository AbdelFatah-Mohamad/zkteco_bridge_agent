# Install the ZKTeco bridge as a Windows service.
# Run from an elevated PowerShell in the folder this script came in.
$ErrorActionPreference = 'Stop'

$Program = 'C:\Program Files\AlshayebCo\ZKBridge'
$Data    = "$env:ProgramData\AlshayebCo\ZKBridge"

New-Item -ItemType Directory -Force -Path "$Program\app", $Data | Out-Null
Copy-Item -Recurse -Force .\zkbridge\* "$Program\app\"
Copy-Item -Force .\winsw.exe "$Program\zkbridge-service.exe"
Copy-Item -Force .\zkbridge.xml "$Program\zkbridge-service.xml"

# ProgramData grants Users:Read by inheritance, which would leave the bridge
# token readable by anyone who can log in to this PC.
icacls $Data /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "BUILTIN\Administrators:(OI)(CI)F" | Out-Null

& "$Program\zkbridge-service.exe" install
& "$Program\zkbridge-service.exe" start

Start-Sleep -Seconds 4
$pin = Get-Content "$Data\ui_pin.txt" -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "ZKTeco bridge installed and started."
Write-Host "  Open        http://127.0.0.1:8709/"
Write-Host "  Access code $pin   <- write this down"
Write-Host ""
Write-Host "This PC needs OUTBOUND 443 to Odoo and OUTBOUND 4370 to each clock."
Write-Host "Nothing inbound: no firewall rule is created and none is needed."
Start-Process "http://127.0.0.1:8709/"
