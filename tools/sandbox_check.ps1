# Unattended check of the FFmpeg download on a brand-new Windows.
#
#   .\tools\sandbox_check.ps1
#
# Builds tools\sandbox_download_check.py into a console exe, starts Windows
# Sandbox, runs it there, waits for the report and lets the Sandbox shut down.
# Close any other Sandbox first (only one can run at a time).

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$py = "$root\.venv\Scripts\python.exe"
$checkDist = "$root\build\dlcheck-dist"
$results = "$root\sandbox\results"

# WindowsSandboxServer is a background service that stays running; only the
# session/client processes mean a Sandbox window is open.
if (Get-Process WindowsSandbox* -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne "WindowsSandboxServer" }) {
    throw "A Windows Sandbox is already open. Close it first."
}

& $py -m PyInstaller --noconfirm --log-level WARN --onefile --console --name dlcheck `
    --paths $root --distpath $checkDist --workpath "$root\build\dlcheck-work" `
    --specpath "$root\build" "$root\tools\sandbox_download_check.py"
if ($LASTEXITCODE) { throw "Building the check failed" }

New-Item -ItemType Directory -Force $results | Out-Null
Remove-Item "$results\download_check.txt" -ErrorAction SilentlyContinue

function Esc([string]$s) { [Security.SecurityElement]::Escape($s) }
$wsb = @"
<Configuration>
  <Networking>Enable</Networking>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>$(Esc $checkDist)</HostFolder>
      <SandboxFolder>C:\Shared\check</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>$(Esc $results)</HostFolder>
      <SandboxFolder>C:\Shared\results</SandboxFolder>
      <ReadOnly>false</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>cmd.exe /c "C:\Shared\check\dlcheck.exe C:\Shared\results\download_check.txt &amp; shutdown /s /t 10"</Command>
  </LogonCommand>
</Configuration>
"@
$wsbPath = "$root\build\dlcheck.wsb"
Set-Content -Path $wsbPath -Value $wsb -Encoding utf8

Write-Host "Starting Windows Sandbox..."
Start-Process $wsbPath
$deadline = (Get-Date).AddMinutes(10)
while ((Get-Date) -lt $deadline) {
    Start-Sleep 5
    $report = "$results\download_check.txt"
    if ((Test-Path $report) -and (Select-String -Path $report -Pattern "^RESULT:|^CRASHED" -Quiet)) {
        Get-Content $report
        exit 0
    }
}
throw "No result after 10 minutes. Look at the Sandbox window."
