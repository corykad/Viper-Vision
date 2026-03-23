@echo off
title Viper Vision Diagnostics
echo Running Viper Vision Test Suite... Please wait.

cd /d "C:\scripts\viper"

REM Run pytest and save the output
pytest viper_test.py > test_results.txt

REM Check the exit code of pytest
if %ERRORLEVEL% EQU 0 (
    echo.
    echo ALL TESTS PASSED!
    PowerShell -Command "Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('Viper Vision diagnostics complete. All tests passed successfully. The system is bulletproof.')"
) else (
    echo.
    echo TESTS FAILED. Opening test_results.txt...
    PowerShell -Command "Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('Alert. Viper Vision diagnostics failed. Opening the test results file now.')"
)

REM This line forces Windows to open the text file instantly
start notepad test_results.txt

pause