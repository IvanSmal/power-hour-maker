# Lets Claude drive Power Hour Maker ("make me a Halloween power hour").
# Registers the MCP server + skill with Claude Code (user-wide) and Claude Desktop.
#   .\install-claude.ps1              both
#   .\install-claude.ps1 -Code        Claude Code only
#   .\install-claude.ps1 -Desktop     Claude Desktop only
#   .\install-claude.ps1 -Uninstall   remove again
param([switch]$Code, [switch]$Desktop, [switch]$Uninstall)
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$argsList = @('-m', 'app.install_claude')
if ($Code) { $argsList += '--code' }
if ($Desktop) { $argsList += '--desktop' }
if ($Uninstall) { $argsList += '--uninstall' }
Push-Location $Root
& (Join-Path $Root '.venv\Scripts\python.exe') @argsList
Pop-Location
