@echo off
rem Nif Vertex Processor - launcher menu.
rem Reads nothing, writes nothing. Starts one of the tools in this folder.
rem Every GUI needs a .nif, so this asks for one when it was not given on the command line.
rem Usage: NifTools.cmd [choice] ["path\to\mesh.nif"]
setlocal EnableExtensions
cd /d "%~dp0"

set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY goto :nopython

set "PYW=%PY%"
for /f "delims=" %%I in ('%PY% -c "import os,sys;print(os.path.dirname(sys.executable))" 2^>nul') do set "PYDIR=%%I"
if defined PYDIR if exist "%PYDIR%\pythonw.exe" set "PYW=%PYDIR%\pythonw.exe"

set "CHOICE=%~1"
set "NIF=%~2"
if not defined CHOICE goto :menu
goto :run

:menu
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
set "CHOICE="
set /p "CHOICE=  Choose [0-7]: "
if not defined CHOICE exit /b 1
if "%CHOICE%"=="0" exit /b 0

:run
if "%CHOICE%"=="1" (
  set "SCRIPT=nif_studio.py"
  goto :gui
)
if "%CHOICE%"=="2" (
  set "SCRIPT=nif_vertex_gui.py"
  goto :gui
)
if "%CHOICE%"=="3" (
  set "SCRIPT=nif_weld_uv_gui.py"
  goto :gui
)
if "%CHOICE%"=="4" goto :selftest
if "%CHOICE%"=="5" goto :audit
if "%CHOICE%"=="6" goto :dump
if "%CHOICE%"=="7" goto :menuinstall
echo   Not a choice: %CHOICE%
exit /b 1

:gui
if defined NIF goto :gui_run
echo.
set /p "NIF=  Mesh file (drag one here): "
:gui_run
if not defined NIF (
  echo   No file given, nothing started.
  exit /b 1
)
if not exist "%NIF%" (
  echo   Not found: %NIF%
  exit /b 1
)
echo   Opening %SCRIPT% with %NIF%
start "" "%PYW%" "%SCRIPT%" "%NIF%"
exit /b 0

:selftest
%PY% "nif_vertex.py" selftest
echo.
pause
exit /b 0

:audit
echo.
set /p "TARGET=  Mesh file or folder: "
if not defined TARGET exit /b 1
%PY% "nif_vertex.py" audit "%TARGET%" --recurse --tsv "%~dp0nif_vertex_audit.tsv"
echo.
echo   Report: %~dp0nif_vertex_audit.tsv
pause
exit /b 0

:dump
echo.
set /p "TARGET=  Mesh file: "
if not defined TARGET exit /b 1
%PY% "nif_block_dump.py" "%TARGET%"
echo.
pause
exit /b 0

:menuinstall
powershell -NoProfile -ExecutionPolicy Bypass -File "Register-NifToolsMenu.ps1" -Install
echo.
pause
exit /b 0

:nopython
echo.
echo   Python 3.10 or newer was not found on PATH.
echo.
echo   Install it from python.org and tick "Add python.exe to PATH",
echo   or run this file from a shell where "python" resolves.
echo.
pause
exit /b 2
