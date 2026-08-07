[CmdletBinding()]
param(
    [Alias("WavPath")]
    [string]$AudioPath,
    [switch]$SkipCalibration,
    [switch]$SkipTaskRegistration
)

$ErrorActionPreference = "Stop"
$ProjectDirectory = $PSScriptRoot
$VirtualEnvironment = Join-Path $ProjectDirectory ".venv"
$Python = Join-Path $VirtualEnvironment "Scripts\python.exe"
$Pythonw = Join-Path $VirtualEnvironment "Scripts\pythonw.exe"
$Watcher = Join-Path $ProjectDirectory "bambu_watcher.py"
$Config = Join-Path $ProjectDirectory "config.json"
$ExampleConfig = Join-Path $ProjectDirectory "config.example.json"
$TaskName = "Bambu Studio Print Finish Watcher"

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python launcher 'py' was not found. Install Python 3.11 or newer from python.org, then rerun setup.ps1."
}
& py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.11 or newer is required. Install it from python.org, then rerun setup.ps1."
}

if (-not (Test-Path -LiteralPath $VirtualEnvironment)) {
    & py -3 -m venv $VirtualEnvironment
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the Python virtual environment."
    }
}
& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "Could not upgrade pip in the virtual environment."
}
& $Python -m pip install -r (Join-Path $ProjectDirectory "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Could not install the watcher dependencies."
}

if (-not (Test-Path -LiteralPath $Config)) {
    Copy-Item -LiteralPath $ExampleConfig -Destination $Config
}

$ConfigData = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
if (-not $AudioPath) {
    $ConfiguredAudio = if ($ConfigData.audio_path) { $ConfigData.audio_path } elseif ($ConfigData.wav_path) { $ConfigData.wav_path } else { "" }
    if ($ConfiguredAudio -and (Test-Path -LiteralPath $ConfiguredAudio)) {
        $AudioPath = $ConfiguredAudio
        Write-Host "Using configured alert sound: $AudioPath"
    } else {
        $AudioPath = Read-Host "Enter the full path to your .wav or .mp3 alert sound"
    }
}
$ResolvedAudio = $null
if ($AudioPath) {
    $ResolvedAudio = Resolve-Path -LiteralPath $AudioPath -ErrorAction SilentlyContinue
}
$AudioExtension = if ($ResolvedAudio) { [IO.Path]::GetExtension($ResolvedAudio.Path).ToLowerInvariant() } else { "" }
if (-not $ResolvedAudio -or $AudioExtension -notin @(".wav", ".mp3")) {
    throw "The alert sound must be an existing .wav or .mp3 file."
}

$ConfigData | Add-Member -NotePropertyName audio_path -NotePropertyValue $ResolvedAudio.Path -Force
$CommonTesseract = "C:\Program Files\Tesseract-OCR\tesseract.exe"
if (-not (Test-Path -LiteralPath $CommonTesseract) -and -not (Get-Command tesseract.exe -ErrorAction SilentlyContinue)) {
    $Winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $Winget) {
        throw "Tesseract OCR is missing and winget is unavailable. Install Tesseract manually, then rerun setup.ps1."
    }
    Write-Host "Tesseract OCR was not found. Installing it with winget..."
    & $Winget.Source install --id UB-Mannheim.TesseractOCR --exact --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "The automatic Tesseract installation failed. Install UB-Mannheim.TesseractOCR manually, then rerun setup.ps1."
    }
}
if (Test-Path -LiteralPath $CommonTesseract) {
    $ConfigData.tesseract_path = $CommonTesseract
} elseif (-not (Get-Command tesseract.exe -ErrorAction SilentlyContinue)) {
    throw "Tesseract installation completed, but tesseract.exe could not be found. Reopen PowerShell and rerun setup.ps1."
}
$ConfigData | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $Config -Encoding utf8

if (-not $SkipCalibration) {
    & $Python $Watcher --config $Config calibrate
    if ($LASTEXITCODE -ne 0) {
        throw "Calibration did not complete successfully."
    }
}

& $Python $Watcher --config $Config validate
if ($LASTEXITCODE -ne 0) {
    throw "Watcher validation failed."
}

if (-not $SkipTaskRegistration) {
    $ActionArguments = "`"$Watcher`" --config `"$Config`" watch"
    $Action = New-ScheduledTaskAction -Execute $Pythonw -Argument $ActionArguments -WorkingDirectory $ProjectDirectory
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $Principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited
    $Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    $Task = New-ScheduledTask -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings
    Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null
}

Write-Host "Setup complete."
Write-Host "Run a diagnostic with: .\.venv\Scripts\python.exe .\bambu_watcher.py diagnostic --show"
