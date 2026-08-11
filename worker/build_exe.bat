@echo off
REM Build PrintHubWorker.exe (same PyInstaller approach as the prototype)
pip install pyinstaller requests pywin32
pyinstaller --noconfirm --onefile --windowed --name PrintHubWorker printhub_worker.py
echo.
echo Done. The exe is in dist\PrintHubWorker.exe
pause
