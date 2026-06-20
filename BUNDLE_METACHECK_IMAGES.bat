@echo off
cd /d "%~dp0"
echo.
echo Bundling MetaCheck Docker images into _system\tools\docker-images.
echo This can take several minutes and several GB of disk space.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "_system\launcher\bundle_metacheck_images.ps1"
if errorlevel 1 (
  echo.
  echo Bundling failed.
  pause
  exit /b 1
)
echo.
echo Bundling complete.
pause
