@echo off
rem Build the native control panel with the MinGW toolchain on D:.
rem Output: ..\OCSub2WB.exe
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "MINGW=D:\dev\mingw64\bin"
if not exist "%MINGW%\g++.exe" (
  echo [x] mingw not found at %MINGW%
  pause
  exit /b 1
)

echo [1/3] resources ...
"%MINGW%\windres.exe" -i app.rc -O coff -o app_res.o
if errorlevel 1 goto :fail

echo [2/3] compile ...
"%MINGW%\g++.exe" -std=c++17 -O2 -s -municode -mwindows -pthread ^
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

:fail
echo [x] build failed
pause
exit /b 1
