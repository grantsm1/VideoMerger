# Runs automatically inside Windows Sandbox when it starts (see VideoMerger.wsb).
# Sets the desktop up the way a friend's PC would look after downloading the app.

$desktop = [Environment]::GetFolderPath("Desktop")
$exe = Join-Path $desktop "VideoMerger.exe"
Copy-Item "C:\Shared\dist\VideoMerger.exe" $exe -Force

# Mark the copy as "downloaded from the internet", exactly like a browser does,
# so Windows shows the same SmartScreen warning your users will see.
Set-Content -Path $exe -Stream Zone.Identifier -Value "[ZoneTransfer]`r`nZoneId=3`r`nHostUrl=https://example.com/VideoMerger.exe"

Copy-Item "C:\Shared\setup\CHECKLIST.txt" (Join-Path $desktop "CHECKLIST.txt") -Force
Start-Process notepad.exe (Join-Path $desktop "CHECKLIST.txt")
