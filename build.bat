@echo off
REM ============================================================
REM  BLEAutoUnlock GUI build script (requires internet access)
REM  Output: dist\BLEAutoUnlock.exe (single file, no console)
REM ============================================================
cd /d "%~dp0"

echo [1/3] Installing runtime dependencies...
python -m pip install -r requirements.txt || goto :error

echo [2/3] Installing build tools...
python -m pip install pyinstaller pyinstaller-hooks-contrib || goto :error

echo [3/3] Building exe...
python -m PyInstaller --noconfirm --clean BLEAutoUnlock.spec || goto :error

echo.
echo Build finished: dist\BLEAutoUnlock.exe
pause
exit /b 0

:error
echo.
echo Build FAILED - please check the error messages above.
pause
exit /b 1
