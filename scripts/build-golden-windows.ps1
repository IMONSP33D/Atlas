<#
.SYNOPSIS
  Atlas: prepare a Windows VM to become a golden image.

.DESCRIPTION
  Run as Administrator inside a freshly installed Windows 10/11 VM on Proxmox
  (q35, OVMF, virtio-scsi, NO network device, virtio-serial port named
  org.atlas.agent). Temporarily attach a NIC for this script, then remove it
  before snapshotting.

  Steps:
    1. Install VirtIO drivers (vioserial is required for the Atlas channel)
    2. Create the interactive 'atlas' user with auto-logon
    3. Install toolchains via winget
    4. Install the Atlas guest agent as a logon scheduled task (Session 1)
    5. Harden: disable Windows Update, Defender real-time (optional), hibernation,
       telemetry, sleep; set fixed resolution for screenshots
    6. Print the build number to record in images.yaml

  After it finishes: shut down, remove the NIC, `qm snapshot <vmid> golden --vmstate 1`.
#>
[CmdletBinding()]
param(
  [string]$AtlasUser = "atlas",
  [string]$AtlasPassword = "ChangeMe-Atlas-2026!",   # local-only, auto-logon; VM has no NIC
  [string]$VirtioIsoDrive = "D:",                     # virtio-win ISO mounted here
  [string]$AgentSource = "E:\atlas-guest-agent",      # folder with atlas-agent.exe (or .py + embedded python)
  [switch]$DisableDefender
)
$ErrorActionPreference = "Stop"
function Step($m) { Write-Host "`n[atlas] $m" -ForegroundColor Cyan }

# 1. VirtIO drivers -----------------------------------------------------------
Step "Installing VirtIO drivers from $VirtioIsoDrive"
$msi = Join-Path $VirtioIsoDrive "virtio-win-gt-x64.msi"
if (Test-Path $msi) {
  Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /qn /norestart" -Wait
} else {
  Write-Warning "virtio-win-gt-x64.msi not found; install vioserial + qxldod manually"
}

# 2. Interactive user + auto-logon --------------------------------------------
Step "Creating user $AtlasUser with auto-logon"
$sec = ConvertTo-SecureString $AtlasPassword -AsPlainText -Force
if (-not (Get-LocalUser -Name $AtlasUser -ErrorAction SilentlyContinue)) {
  New-LocalUser -Name $AtlasUser -Password $sec -PasswordNeverExpires -AccountNeverExpires | Out-Null
}
Add-LocalGroupMember -Group "Administrators" -Member $AtlasUser -ErrorAction SilentlyContinue
$wl = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
Set-ItemProperty $wl AutoAdminLogon "1"
Set-ItemProperty $wl DefaultUserName $AtlasUser
Set-ItemProperty $wl DefaultPassword $AtlasPassword
Set-ItemProperty $wl DefaultDomainName $env:COMPUTERNAME
# Pre-approve elevation for the agent's helper so UAC never blocks a run:
Set-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" ConsentPromptBehaviorAdmin 0
Set-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" EnableLUA 1

# 3. Toolchains ---------------------------------------------------------------
Step "Installing toolchains (requires temporary NIC)"
$pkgs = @(
  "Python.Python.3.12",
  "OpenJS.NodeJS.LTS",
  "Microsoft.PowerShell",
  "Microsoft.DotNet.SDK.8",
  "Rustlang.Rustup",
  "GoLang.Go",
  "Microsoft.VisualStudio.2022.BuildTools",
  "Git.Git",
  "7zip.7zip"
)
foreach ($p in $pkgs) {
  winget install --id $p -e --silent --accept-package-agreements --accept-source-agreements
}
# C/C++ workload for Build Tools (winget installs the bootstrapper only)
$vs = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vs_installer.exe"
if (Test-Path $vs) {
  & $vs modify --installPath "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2022\BuildTools" `
    --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --quiet --norestart
}
& "$env:USERPROFILE\.cargo\bin\rustup.exe" default stable 2>$null
py -3.12 -m pip install --upgrade pip requests numpy pandas pywin32 pillow mss 2>$null

# 4. Guest agent --------------------------------------------------------------
Step "Installing Atlas guest agent"
New-Item -ItemType Directory -Force "C:\Atlas\agent","C:\Atlas\in","C:\Atlas\work" | Out-Null
Copy-Item "$AgentSource\*" "C:\Atlas\agent" -Recurse -Force
$action  = New-ScheduledTaskAction -Execute "C:\Atlas\agent\atlas-agent.exe" -Argument "--channel \\.\Global\org.atlas.agent"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $AtlasUser
$principal = New-ScheduledTaskPrincipal -UserId $AtlasUser -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -RestartCount 99 -RestartInterval (New-TimeSpan -Seconds 10) -ExecutionTimeLimit 0
Register-ScheduledTask -TaskName "AtlasGuestAgent" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

# 5. Harden / quiet ------------------------------------------------------------
Step "Disabling update, sleep, hibernation, telemetry"
Set-Service wuauserv -StartupType Disabled; Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
powercfg /h off
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0
Set-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Windows\DataCollection" AllowTelemetry 0 -Type DWord -Force -ErrorAction SilentlyContinue
if ($DisableDefender) {
  Set-MpPreference -DisableRealtimeMonitoring $true
  New-Item "HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender" -Force | Out-Null
  Set-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender" DisableAntiSpyware 1 -Type DWord
}
# Fixed resolution for predictable screenshots (needs qxldod/virtio-gpu driver)
# Set-DisplayResolution -Width 1920 -Height 1080 -Force   # Server only; on client use display settings once

# 6. Record build ----------------------------------------------------------
Step "Build number for images.yaml:"
$cv = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
"{0}.{1}.{2}.{3}" -f 10, 0, $cv.CurrentBuildNumber, $cv.UBR | Write-Host -ForegroundColor Green
Write-Host "`nNow: shut down, remove the NIC in Proxmox, then: qm snapshot <vmid> golden --vmstate 1" -ForegroundColor Yellow
