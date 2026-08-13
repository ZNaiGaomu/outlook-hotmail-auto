@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [resi] Start the local residential HTTP bridge (127.0.0.1 only, system proxy unchanged)
echo [resi] Path: this project -^> 17890 -^> optional Clash 7890 -^> your residential SOCKS5
echo.
python start_resi_proxy.py --check
echo.
echo [resi] After the bridge is ready, open another terminal and run: python main.py
pause
