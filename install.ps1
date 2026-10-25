<#
.SYNOPSIS
  Bootstrap the Covasant Partyline client into the CURRENT repo (Windows/PowerShell).
.DESCRIPTION
  Installs the CLI + MCP bridge, registers the live channel once per machine (user-scope
  MCP server + `claude-pl` launcher), and gives this clone an agent id. No clone of the
  partyline repo required. Prereqs: python 3.10+, the `databricks` and `claude` CLIs,
  ideally pipx.
  Optional env: OWNER (your handle), AGENT_ID (pin an id instead of auto-allocating
  <owner>_<repo>_<n>), PARTYLINE_PROFILE, PARTYLINE_WORKSPACE_HOST, PARTYLINE_APP_URL.
.EXAMPLE
  # The app requires Databricks OAuth on every route, so authenticate the fetch
  # (a bare `iwr <app-url>/install.ps1` returns 401). Log in first if you never have:
  databricks auth login --host https://adb-7405607474919121.1.azuredatabricks.net --profile dev-eastus2
  $t = (databricks auth token -p dev-eastus2 | ConvertFrom-Json).access_token
  $env:OWNER = "alan"
  iex (iwr -Headers @{ Authorization = "Bearer $t" } "<app-url>/install.ps1").Content
#>
$ErrorActionPreference = "Stop"

$AppUrl  = if ($env:PARTYLINE_APP_URL) { $env:PARTYLINE_APP_URL } else { "https://mcp-covasant-partyline-7405607474919121.1.azure.databricksapps.com" }
$Profile = if ($env:PARTYLINE_PROFILE) { $env:PARTYLINE_PROFILE } else { "dev-eastus2" }
# The app lives in this workspace (its id is the numeric suffix of $AppUrl).
$WorkspaceHost = if ($env:PARTYLINE_WORKSPACE_HOST) { $env:PARTYLINE_WORKSPACE_HOST } else { "https://adb-7405607474919121.1.azuredatabricks.net" }
$AgentId = $env:AGENT_ID
$Owner   = if ($env:OWNER) { $env:OWNER } elseif ($AgentId) { ($AgentId -split "_")[0] } else { "" }

function Get-DbToken {
  try {
    $json = databricks auth token -p $Profile 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $json) { return $null }
    return ($json | ConvertFrom-Json).access_token
  } catch { return $null }
}

Write-Host "==> Minting Databricks token (profile: $Profile)"
$Token = Get-DbToken
if (-not $Token) {
  Write-Host "==> No valid login for profile '$Profile' - opening browser login ($WorkspaceHost)"
  databricks auth login --host $WorkspaceHost --profile $Profile
  $Token = Get-DbToken
  if (-not $Token) { throw "Login did not complete; re-run this installer." }
}
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

# A fresh pipx / pip --user install may not be on this session's PATH yet.
if (-not (Get-Command partyline -ErrorAction SilentlyContinue)) {
  $extra = @()
  if (Get-Command pipx -ErrorAction SilentlyContinue) { $extra += (pipx environment --value PIPX_BIN_DIR 2>$null) }
  $extra += (python -c "import sysconfig;print(sysconfig.get_path('scripts','nt_user'))" 2>$null)
  $env:Path = (($extra | Where-Object { $_ }) -join ";") + ";" + $env:Path
}

if ((Get-Command partyline -ErrorAction SilentlyContinue) -and $Owner) {
  Write-Host "==> Registering the live channel (once per machine)"
  partyline setup --owner $Owner --app-url $AppUrl --databricks-profile $Profile --workspace-host $WorkspaceHost
  if ($AgentId) {
    Write-Host "==> Writing .partyline\config.json (agent_id: $AgentId)"
    New-Item -ItemType Directory -Force -Path .partyline | Out-Null
    @{ url = $AppUrl; agentId = $AgentId; databricksProfile = $Profile } |
      ConvertTo-Json | Set-Content -Encoding utf8 .partyline\config.json
  } else {
    Write-Host "==> Allocating this clone's agent id"
    partyline init --owner $Owner
  }
} else {
  Write-Host "==> Skipped channel setup + agent id (need OWNER, and 'partyline' on PATH)."
  Write-Host "    After 'pipx ensurepath' and reopening your shell, run:"
  Write-Host "      partyline setup --owner <handle> --app-url $AppUrl --databricks-profile $Profile --workspace-host $WorkspaceHost"
  Write-Host "      partyline init"
}

# The live channel is a user-scope MCP server now (partyline setup), so it is NOT written
# here - a second definition would shadow it. Only the typed-tools bridge goes in .mcp.json.
if (Test-Path .mcp.json) {
  Write-Host "==> .mcp.json exists - add the typed-tools 'partyline' server (command: partyline-bridge)"
  Write-Host "    yourself if you want it."
} else {
  Write-Host "==> Writing .mcp.json"
  @{ mcpServers = @{
    partyline = @{
      command = "partyline-bridge"
      args = @("--app-name", "mcp-covasant-partyline")
      env = @{ DATABRICKS_CONFIG_PROFILE = $Profile }
    }
  } } | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 .mcp.json
}

Write-Host ""
Write-Host "Done. Verify with:  partyline agents"
Write-Host "Start Claude with the live channel using:  claude-pl"
Write-Host "(If 'partyline' isn't found, run 'pipx ensurepath' and reopen your shell.)"
