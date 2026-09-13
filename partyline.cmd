@echo off
setlocal enabledelayedexpansion
REM partyline — Windows wrapper around the Covasant Partyline CLI.
REM Resolves the app URL from .partyline\config.json (or %PARTYLINE_URL%) and mints a
REM Databricks OAuth token (or reuses %PARTYLINE_TOKEN%), then forwards all args to the
REM CLI. Examples:
REM   partyline.cmd agents
REM   partyline.cmd post alan_partyline_1 --type status --payload "hi"

set "ROOT=%~dp0"
set "CONFIG=%ROOT%.partyline\config.json"

if "%PARTYLINE_URL%"=="" if exist "%CONFIG%" (
  for /f "usebackq delims=" %%u in (`python -c "import json;print(json.load(open(r'%CONFIG%')).get('url','') or '')"`) do set "PARTYLINE_URL=%%u"
)

if "%PARTYLINE_TOKEN%"=="" (
  REM App-specific config profile wins over ambient DATABRICKS_CONFIG_PROFILE (which may
  REM point at a different workspace and would 401 the app).
  set "PROFILE="
  if exist "%CONFIG%" (
    for /f "usebackq delims=" %%p in (`python -c "import json;print(json.load(open(r'%CONFIG%')).get('databricksProfile','') or '')"`) do set "PROFILE=%%p"
  )
  if "!PROFILE!"=="" set "PROFILE=%DATABRICKS_CONFIG_PROFILE%"
  if "!PROFILE!"=="" set "PROFILE=dev-eastus2"
  for /f "usebackq delims=" %%t in (`databricks auth token -p !PROFILE! ^| python -c "import sys,json;print(json.load(sys.stdin)['access_token'])"`) do set "PARTYLINE_TOKEN=%%t"
)

python -m covasant_partyline.cli %*
REM Propagate the CLI's exit code: a bare endlocal would swallow it and always report 0.
REM The monitor wake recipe depends on the difference (0 = mention arrived, 2 = timeout).
REM %ERRORLEVEL% expands when this line is parsed, i.e. to python's exit code.
endlocal & exit /b %ERRORLEVEL%
