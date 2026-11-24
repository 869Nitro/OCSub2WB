@echo off
rem Build the native control panel with the MinGW toolchain.
rem Output: ..\OCSub2WB.exe
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
if /I "%~1"=="--ci" set "CI=1"

rem Prefer an explicit MINGW directory, then PATH, then the local D: toolchain.
set "GXX="
set "WINDRES="
if defined MINGW (
  if exist "%MINGW%\g++.exe" set "GXX=%MINGW%\g++.exe"
  if exist "%MINGW%\windres.exe" set "WINDRES=%MINGW%\windres.exe"
)
if not defined GXX where g++ >nul 2>nul && set "GXX=g++"
if not defined WINDRES where windres >nul 2>nul && set "WINDRES=windres"
if not defined GXX if exist "D:/dev/mingw64/bin/g++.exe" set "GXX=D:/dev/mingw64/bin/g++.exe"
if not defined WINDRES if exist "D:/dev/mingw64/bin/windres.exe" set "WINDRES=D:/dev/mingw64/bin/windres.exe"

if not defined GXX goto :notool
if not defined WINDRES goto :notool

echo [1/3] resources ...
"%WINDRES%" -i app.rc -O coff -o app_res.o
if errorlevel 1 goto :fail

echo [2/3] compile ...
"%GXX%" -std=c++17 -O2 -s -municode -mwindows -pthread ^
  -finput-charset=UTF-8 -fexec-charset=UTF-8 ^
  ocgo_panel.cpp app_res.o -o ..\OCSub2WB.exe ^
  -lwinhttp -lcomctl32 -lgdi32 -luser32 -lshell32 -lole32 -lws2_32 ^
  -static -static-libgcc -static-libstdc++ -lwinpthread
if errorlevel 1 goto :fail

echo [3/3] manifest ...
python "%~dp0patch_manifest.py" "%~dp0..\OCSub2WB.exe" "%~dp0app.manifest"
if errorlevel 1 goto :fail

echo [ok] ..\OCSub2WB.exe
for %%F in (..\OCSub2WB.exe) do echo     size: %%~zF bytes
exit /b 0

:notool
echo [x] g++/windres not found. Install MSYS2 MinGW or set MINGW to the bin directory.
if not defined CI pause
exit /b 1

:fail
echo [x] build failed
if not defined CI pause
exit /b 1
