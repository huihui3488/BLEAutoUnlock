@echo off
chcp 65001 >nul
REM ============================================================
REM  BLEAutoUnlock GUI 打包脚本（需要联网安装依赖）
REM  运行后生成 dist\BLEAutoUnlock.exe（单文件、无控制台窗口）
REM ============================================================
cd /d "%~dp0"

echo [1/3] 安装运行依赖...
python -m pip install -r requirements.txt || goto :error

echo [2/3] 安装打包工具...
python -m pip install pyinstaller pyinstaller-hooks-contrib || goto :error

echo [3/3] 开始打包...
python -m PyInstaller --noconfirm --clean BLEAutoUnlock.spec || goto :error

echo.
echo 打包完成：dist\BLEAutoUnlock.exe
pause
exit /b 0

:error
echo.
echo 打包失败，请检查上面的错误信息。
pause
exit /b 1
