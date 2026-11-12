@echo off
REM Starts the Megapro Song Manager from the source files (needs Python 3.9+; numpy for song preview)
cd /d "%~dp0"
py -c "import numpy" 2>nul || py -m pip install numpy
py songmgr.py %*
pause
