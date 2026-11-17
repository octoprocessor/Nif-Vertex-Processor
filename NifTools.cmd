@echo off
rem Nif Vertex Processor - launcher menu.
rem Reads nothing, writes nothing. Starts one of the tools in this folder.
setlocal EnableExtensions
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY goto :nopython

set "PYW=%PY%"
for /f "delims=" %%I in ('%PY% -c "import os,sys;print(os.path.dirname(sys.executable))" 2^>nul') do set "PYDIR=%%I"
if defined PYDIR if exist "%PYDIR%\pythonw.exe" set "PYW=%PYDIR%\pythonw.exe"

set "CHOICE=%~1"
if not "%CHOICE%"=="" goto :run
cls
echo.
echo   Nif Vertex Processor
echo   -------------------
echo.
echo   1  Shape studio              (nif_studio.py)
echo   2  Vertex alpha mask         (nif_vertex_gui.py)
echo   3  Weld + reproject UV       (nif_weld_uv_gui.py)
echo.
echo   4  Self-test                 (nif_vertex.py selftest)
echo   5  Audit a mesh or folder    (nif_vertex.py audit)
echo   6  What is in this mesh      (nif_block_dump.py)
echo.
echo   7  Install right-click menu  (per-user, no shortcuts)
echo   0  Exit
echo.
set /p "CHOICE=  Choose [0-7]: "
if "%CHOICE%"=="" exit /b 1
if "%CHOICE%"=="0" exit /b 0

:run
if "%CHOICE%"=="1" (
  start "" "%PYW%" "nif_studio.py" %2 %3
  exit /b 0
)
if "%CHOICE%"=="2" (
  start "" "%PYW%" "nif_vertex_gui.py" %2 %3
  exit /b 0
)
if "%CHOICE%"=="3" (
  start "" "%PYW%" "nif_weld_uv_gui.py" %2 %3
  exit /b 0
)
if "%CHOICE%"=="4" (
  %PY% "nif_vertex.py" selftest
  echo.
  pause
  exit /b 0
)
if "%CHOICE%"=="5" (
  set /p "TARGET=  Mesh file or folder: "
  %PY% "nif_vertex.py" audit "%TARGET%" --recurse
  echo.
  pause
  exit /b 0
)
if "%CHOICE%"=="6" (
  set /p "TARGET=  Mesh file: "
  %PY% "nif_block_dump.py" "%TARGET%"
  echo.
  pause
  exit /b 0
)
if "%CHOICE%"=="7" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "Register-NifToolsMenu.ps1" -Install
  echo.
  pause
  exit /b 0
)
echo   Not a choice: %CHOICE%
exit /b 1

:nopython
echo.
echo   Python 3.10 or newer was not found on PATH.
echo.
echo   Install it from python.org and tick "Add python.exe to PATH",
echo   or run this file from a shell where "python" resolves.
echo.
pause
exit /b 2
