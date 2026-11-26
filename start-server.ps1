# Compatibility entry point for existing Windows commands.
param([int]$Port)

& (Join-Path $PSScriptRoot "run-scripts/start-server.ps1") @PSBoundParameters
