@echo off
REM Build PrintHubWorker.exe
REM --hidden-import win32timezone: pywin32 imports it lazily at print time,
REM so PyInstaller cannot detect it. Without it every print crashes with
REM "No module named 'win32timezone'" AFTER the page has been sent to the
REM printer, which made jobs reprint endlessly.
pip install pyinstaller requests pywin32
pyinstaller --noconfirm --onefile --windowed --name PrintHubWorker ^
  --hidden-import win32timezone ^
  --hidden-import win32print ^
  --hidden-import win32api ^
  --hidden-import win32con ^
  printhub_worker.py
echo.
echo Done. The exe is in dist\PrintHubWorker.exe
pause
