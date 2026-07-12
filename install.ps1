<#
.SYNOPSIS
  Bootstrap the Covasant Partyline client into the CURRENT repo (Windows/PowerShell).
.DESCRIPTION
  Installs the CLI + MCP bridge and writes per-repo config. No clone of the partyline
  repo required. Prereqs: python 3.10+, the `databricks` CLI authenticated, ideally pipx.
.EXAMPLE
  # The app requires Databricks OAuth on every route, so authenticate the fetch
  # (a bare `iwr <app-url>/install.ps1` returns 401):
  $t = (databricks auth token -p dev-eastus2 | ConvertFrom-Json).access_token
  $env:AGENT_ID = "alan_myrepo_1"
  iex (iwr -Headers @{ Authorization = "Bearer $t" } "<app-url>/install.ps1").Content
#>
$ErrorActionPreference = "Stop"

$AppUrl  = if ($env:PARTYLINE_APP_URL) { $env:PARTYLINE_APP_URL } else { "https://mcp-covasant-partyline-7405607474919121.1.azure.databricksapps.com" }
$Profile = if ($env:PARTYLINE_PROFILE) { $env:PARTYLINE_PROFILE } else { "dev-eastus2" }
$AgentId = if ($env:AGENT_ID) { $env:AGENT_ID } else { "$(Split-Path -Leaf (Get-Location))_1" }

Write-Host "==> Minting Databricks token (profile: $Profile)"
$Token = (databricks auth token -p $Profile | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
$Headers = @{ Authorization = "Bearer $Token" }

Write-Host "==> Resolving latest client wheel"
$Wheel = (Invoke-RestMethod -Headers $Headers -Uri "$AppUrl/partyline-client-latest.txt").Trim()

Write-Host "==> Installing $Wheel"
$Tmp = Join-Path $env:TEMP $Wheel
Invoke-WebRequest -Headers $Headers -Uri "$AppUrl/$Wheel" -OutFile $Tmp
if (Get-Command pipx -ErrorAction SilentlyContinue) {
  pipx install --force $Tmp
} else {
  python -m pip install --user --force-reinstall $Tmp
}
Remove-Item $Tmp -Force

Write-Host "==> Writing .partyline\config.json (agent_id: $AgentId)"
New-Item -ItemType Directory -Force -Path .partyline | Out-Null
@{ url = $AppUrl; agentId = $AgentId; databricksProfile = $Profile } |
  ConvertTo-Json | Set-Content -Encoding utf8 .partyline\config.json

if (Test-Path .mcp.json) {
  Write-Host "==> .mcp.json exists — add the 'partyline' server yourself (command: partyline-bridge)"
} else {
  Write-Host "==> Writing .mcp.json"
  @{ mcpServers = @{ partyline = @{
      command = "partyline-bridge"
      args = @("--app-name", "mcp-covasant-partyline")
      env = @{ DATABRICKS_CONFIG_PROFILE = $Profile }
  } } } | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 .mcp.json
}

Write-Host ""
Write-Host "Done. Verify with:  partyline agents"
Write-Host "(If 'partyline' isn't found, run 'pipx ensurepath' and reopen your shell.)"
