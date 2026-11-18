@echo off
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "NLTK_DATA=%~dp0.cache\nltk_data"
set "DEBUG=false"
pushd "%~dp0finrobot_autogen"
"%~dp0.venv\Scripts\python.exe" run_agent.py %*
set "FINROBOT_EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %FINROBOT_EXIT_CODE%
