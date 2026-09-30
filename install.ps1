# Jev Desktop installer: Python environment, packages, API keys, shortcuts, optional autostart.
# Run it through install.bat (double-click), or:  powershell -ExecutionPolicy Bypass -File install.ps1
#
#   -Quiet        no questions: keep existing keys, create the Start Menu shortcut, no autostart
#   -Startup      start Jev when you log in to Windows (and -NoStartup to stop that)
#   -Uninstall    remove Jev's shortcuts and autostart (the folder itself is left alone)
#   -NoLaunch     don't start Jev at the end

param([switch]$Quiet, [switch]$Startup, [switch]$NoStartup, [switch]$Uninstall, [switch]$NoLaunch)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Venv = Join-Path $Root ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$VenvPythonw = Join-Path $Venv "Scripts\pythonw.exe"
$Launcher = Join-Path $Root "Jev.pyw"
$Icon = Join-Path $Root "assets\jev.ico"
$EnvFile = Join-Path $Root ".env"
$Shortcuts = @{
    StartMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "Jev.lnk"
    Desktop   = Join-Path ([Environment]::GetFolderPath("Desktop")) "Jev.lnk"
    Startup   = Join-Path ([Environment]::GetFolderPath("Startup")) "Jev.lnk"
}

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Ask($question, [bool]$default) {
    if ($Quiet) { return $default }
    $hint = if ($default) { "[Y/n]" } else { "[y/N]" }
    $answer = Read-Host "$question $hint"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $default }
    return $answer.Trim().ToLower().StartsWith("y")
}
function New-Shortcut($path) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $VenvPythonw
    $lnk.Arguments = "`"$Launcher`""
    $lnk.WorkingDirectory = $Root
    $lnk.IconLocation = $Icon
    $lnk.Description = "Jev desktop assistant"
    $lnk.Save()
}
function Stop-Jev {
    Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
        Where-Object { $_.CommandLine -match [regex]::Escape($Root) -or $_.CommandLine -match "jev_assistant" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

if ($Uninstall) {
    Step "Removing Jev's shortcuts"
    Stop-Jev
    foreach ($p in $Shortcuts.Values) { if (Test-Path $p) { Remove-Item $p; Write-Host "removed $p" } }
    Write-Host "Done. Delete the folder $Root to remove Jev completely."
    exit 0
}

# ---------------------------------------------------------------- Python
Step "Finding Python 3.12 or newer"
$python = $null
foreach ($candidate in @(@("py", "-3.14"), @("py", "-3.13"), @("py", "-3.12"), @("python"), @("python3"))) {
    try {
        $version = & $candidate[0] @($candidate[1..9] + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")) 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$version -ge [version]"3.12") { $python = $candidate; break }
    } catch { }
}
if (-not $python) {
    Write-Host "Python 3.12+ wasn't found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again." -ForegroundColor Red
    exit 1
}
Write-Host "using $($python -join ' ') (Python $version)"

# ---------------------------------------------------------------- packages
if (-not (Test-Path $VenvPython)) {
    Step "Creating the virtual environment (.venv)"
    & $python[0] @($python[1..9] + @("-m", "venv", $Venv))
}
Step "Installing packages (a few minutes the first time)"
& $VenvPython -m pip install --upgrade pip --quiet
& $VenvPython -m pip install -r (Join-Path $Root "requirements.txt") --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "Package installation failed (see above)." -ForegroundColor Red; exit 1 }

if (Ask "Install offline voice recognition too? (Hinglish model, ~7 min, ~350 MB; not needed with Sarvam or Gemini)" $false) {
    & $VenvPython -m pip install -r (Join-Path $Root "requirements-local-voice.txt") --quiet
    & $VenvPython -m jev_assistant.voice.setup_local
}

# ---------------------------------------------------------------- API keys
Step "API keys (stored only in .env on this PC; press Enter to skip one)"
if (-not (Test-Path $EnvFile)) { Copy-Item (Join-Path $Root ".env.example") $EnvFile }
$keys = [ordered]@{
    TYPESAFE_API_KEY  = "TypeSafe (Jev's fast routing) - recommended - https://console.typesafe.ai"
    GEMINI_API_KEY    = "Google Gemini (answers questions, voice fallback) - recommended - https://aistudio.google.com/apikey"
    SARVAM_API_KEY    = "Sarvam (best Indian/Hinglish voice) - optional - https://dashboard.sarvam.ai"
    SPOTIFY_CLIENT_ID = "Spotify app playback (Premium) - optional - see README > Spotify"
}
$envText = Get-Content $EnvFile -Raw -Encoding UTF8
foreach ($name in $keys.Keys) {
    $match = [regex]::Match($envText, "(?m)^$name=(.*)$")
    $current = if ($match.Success) { $match.Groups[1].Value.Trim() } else { "" }
    if ($current) { Write-Host "$name is set"; continue }
    if ($Quiet) { Write-Host "$name not set"; continue }
    $value = Read-Host "$($keys[$name])`n  $name"
    if ([string]::IsNullOrWhiteSpace($value)) { continue }
    if ($match.Success) { $envText = [regex]::Replace($envText, "(?m)^$name=.*$", "$name=$($value.Trim())") }
    else { $envText = $envText.TrimEnd() + "`r`n$name=$($value.Trim())`r`n" }
}
[IO.File]::WriteAllText($EnvFile, $envText, (New-Object Text.UTF8Encoding $false))

$spotifySet = [regex]::IsMatch($envText, "(?m)^SPOTIFY_CLIENT_ID=\S+")
if ($spotifySet -and -not (Test-Path (Join-Path $Root "data\spotify_token.json"))) {
    if (Ask "Connect your Spotify account now? (opens your browser once)" $true) {
        & $VenvPython -m jev_assistant --spotify-login
    }
}

# ---------------------------------------------------------------- shortcuts
Step "Shortcuts"
New-Shortcut $Shortcuts.StartMenu
Write-Host "Start Menu: Jev"
if (Ask "Add a Jev shortcut to the Desktop?" $false) { New-Shortcut $Shortcuts.Desktop; Write-Host "Desktop: Jev" }

$autostart = if ($Startup) { $true } elseif ($NoStartup) { $false } else { Ask "Start Jev automatically when you log in to Windows?" $true }
if ($autostart) { New-Shortcut $Shortcuts.Startup; Write-Host "Jev will start when you log in." }
elseif (Test-Path $Shortcuts.Startup) { Remove-Item $Shortcuts.Startup; Write-Host "Autostart removed." }

# ---------------------------------------------------------------- done
Step "Done"
Write-Host "Check your microphone and voices any time with:  .venv\Scripts\python -m jev_assistant --voice-check"
if (-not $NoLaunch) {
    Stop-Jev
    Start-Process -FilePath $VenvPythonw -ArgumentList "`"$Launcher`"" -WorkingDirectory $Root
    Write-Host "Jev is starting: look for the orb near the top-right of your screen. Click it to type; Ctrl+Alt+J to talk."
}
