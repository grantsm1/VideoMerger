# Writes sandbox\VideoMerger.wsb. Windows Sandbox needs full folder paths, so
# run this again if you move the project folder.
#
#   .\tools\make_sandbox.ps1
#
# Then double-click sandbox\VideoMerger.wsb to start a clean Windows with the app.

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$sandbox = Join-Path $root "sandbox"

if (-not (Test-Path "$root\dist\VideoMerger.exe")) { throw "Build first: .\build.ps1" }
if (-not (Test-Path "$sandbox\test-videos\01 Beach day.mp4")) {
    & "$root\.venv\Scripts\python.exe" "$root\tools\make_test_videos.py"
}

function Esc([string]$s) { [Security.SecurityElement]::Escape($s) }

$wsb = @"
<Configuration>
  <Networking>Enable</Networking>
  <MemoryInMB>4096</MemoryInMB>
  <ClipboardRedirection>Enable</ClipboardRedirection>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>$(Esc "$root\dist")</HostFolder>
      <SandboxFolder>C:\Shared\dist</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>$(Esc "$sandbox\inside")</HostFolder>
      <SandboxFolder>C:\Shared\setup</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>$(Esc "$sandbox\test-videos")</HostFolder>
      <SandboxFolder>C:\Users\WDAGUtilityAccount\Desktop\Test videos</SandboxFolder>
      <ReadOnly>false</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Shared\setup\setup.ps1</Command>
  </LogonCommand>
</Configuration>
"@

$out = Join-Path $sandbox "VideoMerger.wsb"
Set-Content -Path $out -Value $wsb -Encoding utf8
Write-Host "Wrote $out"
