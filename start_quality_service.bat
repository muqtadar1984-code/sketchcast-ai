@echo off
REM SketchCast AI - Automatic Quality Checking Service Launcher
REM Starts the background quality checker service

setlocal enabledelayexpand

echo.
echo ════════════════════════════════════════════════════════════════════
echo SketchCast AI - Automatic Quality Checking Service
echo ════════════════════════════════════════════════════════════════════
echo.
echo This launcher will start the continuous quality checking service.
echo.
echo What it does:
echo   • Monitors all Python files for changes
echo   • Runs quality checks automatically (3 second cooldown)
echo   • Reports issues in real-time
echo   • Applies auto-fixes (imports, formatting)
echo   • Communicates findings back to Claude
echo.
echo The service will run in the background until you close this window.
echo.
pause

echo Starting Background Quality Checker Service...
echo.

python background_quality_checker.py

if errorlevel 1 (
    echo.
    echo Error: Failed to start service
    echo Make sure you're in the project root directory
    pause
)

exit /b
