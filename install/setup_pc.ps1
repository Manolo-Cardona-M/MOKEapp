<#
Installs MOKEapp on a new PC.

  1. Install from ni.com: NI-VISA, NI-488.2 (for a GPIB multimeter) and NI-DAQmx (a version that lists
     your DAQ as supported). Install Python 3.12 or newer from python.org, and Git.
  2. git clone https://github.com/Manolo-Cardona-M/MOKEapp.git
  3. In PowerShell, from the MOKEapp folder:   powershell -ExecutionPolicy Bypass -File install\setup_pc.ps1
  4. Open MOKEapp from the desktop icon: the first time it asks for the instruments (Instruments > Setup).

Run it again at any time: it only reports what is missing and refreshes the Python packages.
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$problems = @()

function Test-NiKey($name) {
    foreach ($base in "HKLM:\SOFTWARE\National Instruments", "HKLM:\SOFTWARE\WOW6432Node\National Instruments") {
        if (Test-Path (Join-Path $base $name)) { return $true }
    }
    return $false
}

Write-Host "== MOKEapp setup in $root" -ForegroundColor Cyan

# --- NI drivers
foreach ($d in @(@{Key = "NI-VISA"; Why = "talks to the multimeter (GPIB/USB)"},
                 @{Key = "NI-488.2"; Why = "drives a GPIB-USB adapter (not needed for a USB multimeter)"},
                 @{Key = "NI-DAQmx"; Why = "drives the NI DAQ"})) {
    if (Test-NiKey $d.Key) { Write-Host "  [ok]      $($d.Key)" }
    else { Write-Host "  [missing] $($d.Key): $($d.Why). Install it from ni.com." -ForegroundColor Yellow
           $problems += $d.Key }
}

# --- Python
$py = $null
foreach ($cmd in @("py -3", "python")) {
    try {
        $v = Invoke-Expression "$cmd -c `"import sys; print('%d.%d' % sys.version_info[:2])`"" 2>$null
        if ($v -and [version]$v -ge [version]"3.12") { $py = $cmd; break }
    } catch {}
}
if (-not $py) {
    Write-Host "  [missing] Python 3.12+ (python.org; tick 'Add python.exe to PATH')." -ForegroundColor Red
    exit 1
}
Write-Host "  [ok]      Python ($py)"

# --- virtual environment and packages
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating .venv ..."
    Invoke-Expression "$py -m venv .venv"
}
Write-Host "Installing Python packages (requirements.txt) ..."
& .venv\Scripts\python.exe -m pip install --upgrade pip --quiet
& .venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "pip failed: see the messages above." -ForegroundColor Red; exit 1 }

# --- what the drivers can see right now
Write-Host "Instruments visible now:" -ForegroundColor Cyan
& .venv\Scripts\python.exe -c @"
import sys
sys.path.insert(0, '.')
from mokeapp.drivers import discovery
try:
    print('  VISA:', ', '.join(discovery.visa_resources()) or 'nothing (check cables, power, drivers)')
except Exception as e:
    print('  VISA not usable:', e)
try:
    import nidaqmx.system
    v = nidaqmx.system.System.local().driver_version
    print(f'  NI-DAQmx {v.major_version}.{v.minor_version}.{v.update_version}')
    for d in discovery.daq_devices():
        print('  DAQ', d.name, '-', d.describe())
except Exception as e:
    print('  NI-DAQmx not usable:', e)
"@

# --- desktop shortcut
$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "MOKEapp.lnk"
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$s.TargetPath = Join-Path $root ".venv\Scripts\pythonw.exe"
$s.Arguments = "`"$(Join-Path $root 'main.py')`""
$s.WorkingDirectory = $root
$s.IconLocation = "$(Join-Path $root 'mokeapp\gui\assets\icon.ico'),0"
$s.WindowStyle = 3
$s.Description = "MOKEapp"
$s.Save()
Write-Host "Desktop shortcut: $lnk"

if ($problems.Count) {
    Write-Host "Done, but install first: $($problems -join ', ')" -ForegroundColor Yellow
} else {
    Write-Host "Done. Open MOKEapp from the desktop; the first time choose the instruments in Setup." -ForegroundColor Green
}
