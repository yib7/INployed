@echo off
REM ===========================================================================
REM  Open the INployed dashboard - just double-click this file.
REM
REM  No terminal needed. This runs local\open_dashboard.pyw, which finds your
REM  latest scored jobs (or your synced Job-data folder) and opens the window.
REM  Tip: right-click this file -> "Send to" -> "Desktop (create shortcut)" to
REM  get a one-click icon on your desktop.
REM ===========================================================================
setlocal
set "APP=%~dp0local\open_dashboard.pyw"

REM If the README's optional project venv exists, use its windowed Python so
REM the dependencies installed there are found (a global pythonw wouldn't see them).
if exist "%~dp0venv\Scripts\pythonw.exe" (
    start "" "%~dp0venv\Scripts\pythonw.exe" "%APP%"
    goto :end
)

REM Prefer the windowed Python (no console window). Fall back through the
REM Windows "py" launcher, then plain python as a last resort.
where pythonw >nul 2>nul
if %ERRORLEVEL%==0 (
    start "" pythonw "%APP%"
    goto :end
)
where pyw >nul 2>nul
if %ERRORLEVEL%==0 (
    start "" pyw "%APP%"
    goto :end
)
where py >nul 2>nul
if %ERRORLEVEL%==0 (
    start "" py -3 "%APP%"
    goto :end
)
start "" python "%APP%"

:end
endlocal
