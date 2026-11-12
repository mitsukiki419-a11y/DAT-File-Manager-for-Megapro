@echo off
REM Builds KaraokeSongManager.exe (single file) on Windows. Requires Python 3.9+ from python.org (tick "Add to PATH").
REM Optional: put ffmpeg.exe in this folder first and it is bundled (needed to turn chorus clips into .wav and to
REM convert music videos that are not already DivX/Xvid AVI).
setlocal
cd /d "%~dp0"
where python >nul 2>nul || (echo Python not found. Install it from https://www.python.org/downloads/ & pause & exit /b 1)
python -m pip install --upgrade pyinstaller numpy || (echo pip failed & pause & exit /b 1)
set EXTRA=
if exist ffmpeg.exe (echo Bundling ffmpeg.exe & set EXTRA=--add-binary "ffmpeg.exe;.")
if exist kecpu.dll set EXTRA=%EXTRA% --add-binary "kecpu.dll;."
python -m PyInstaller --onefile --clean --name KaraokeSongManager --hidden-import livesynth --hidden-import sf2synth --hidden-import winperf %EXTRA% songmgr.py || (echo build failed & pause & exit /b 1)
echo.
echo Built: dist\KaraokeSongManager.exe
echo Put dreamsam_2695.sf2 next to it for song preview.
if not exist ffmpeg.exe echo Note: without ffmpeg.exe chorus clips are saved as .wma only, only .wma files can be added as a chorus, and only DivX/Xvid AVI music videos can be added.
pause
