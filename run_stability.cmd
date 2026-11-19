@echo off
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "DEBUG=false"
set "NLTK_DATA=%~dp0.cache\nltk_data"
pushd "%~dp0"
"%~dp0.venv\Scripts\python.exe" finrobot_autogen\experiments\run_stability.py %*
set "FINROBOT_EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %FINROBOT_EXIT_CODE%
