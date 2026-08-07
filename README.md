# Bambu Studio Print-Finish Watcher

This Windows utility watches a fixed Bambu Studio status area and plays a WAV or MP3 file once after it sees `Printing` change to `Finished`. It does not connect to the printer or the Bambu account.

## Requirements

- Windows 10 or 11
- Python 3.11 or newer
- A `.wav` or `.mp3` alert sound
- Bambu Studio kept visible in the calibrated location while Windows remains unlocked

Tesseract OCR is installed automatically through Windows Package Manager (`winget`) when it is missing. If `winget` is unavailable, install [Tesseract OCR for Windows](https://github.com/UB-Mannheim/tesseract/wiki) manually.

## Install

Open PowerShell in this folder and run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
```

The setup script creates `.venv`, installs the Python packages, installs Tesseract when needed, asks for the alert sound, opens the calibration selector, validates the installation, and creates a scheduled task that runs at logon only in your interactive session. It reuses a previously configured sound when setup is rerun.

During calibration, the PowerShell window minimizes before the screenshot is taken. Draw a tight rectangle around the text that displays `Printing` or `Finished`, then press Enter. PowerShell is restored afterward. If the status is elsewhere or the display layout changes, recalibrate:

```powershell
.\.venv\Scripts\python.exe .\bambu_watcher.py calibrate
```

## Test and run

Capture the region once, display it, and print the OCR result:

```powershell
.\.venv\Scripts\python.exe .\bambu_watcher.py diagnostic --show
```

The diagnostic command also minimizes PowerShell before capturing the screen. PowerShell remains minimized while the diagnostic image is displayed and returns after that window closes.

Run the watcher in a visible console for testing:

```powershell
.\.venv\Scripts\python.exe .\bambu_watcher.py watch
```

Press Ctrl+C to stop the console watcher. The scheduled copy starts automatically at the next sign-in; it can also be started or stopped in Windows Task Scheduler under **Bambu Studio Print Finish Watcher**.

Configuration is stored in `config.json`. Paths without a drive letter are resolved relative to that file. Logs rotate under `logs`, and `data/state.json` prevents an old finished job from alerting again after a restart.

The default requires two consecutive `Finished` readings. At the 30-second polling interval, the alert therefore arrives roughly 30–60 seconds after completion. Blank captures, unreadable OCR, and unrelated text do not trigger the sound.

## Troubleshooting

- If diagnostic OCR is poor, select a tighter rectangle containing only the status text.
- If you move Bambu Studio, change monitors, alter Windows display scaling, or change screen resolution, recalibrate.
- The application must be visible and Windows must be unlocked. A minimized, covered, sleeping, or locked desktop cannot be read reliably.
- If automatic Tesseract installation fails, run `winget install --id UB-Mannheim.TesseractOCR --exact` and rerun setup.
- Check `logs\bambu-watcher.log` for captured text, confidence values, state changes, and errors.
