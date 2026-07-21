[CmdletBinding()]
param(
    [string]$ImagePath = $env:AXI_FLASH_IMAGE_PATH,
    [string]$NrfjprogPath = $env:AXI_FLASH_NRFJPROG_PATH,
    [string]$JLinkDllPath = $env:AXI_FLASH_JLINK_DLL_PATH,
    [string]$ProbeId = $env:POC3A_JLINK_ID,
    [switch]$NoVerify,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Resolve-RequiredFile {
    param(
        [string]$Path,
        [string]$Label
    )
    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "$Label is empty"
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label not found: $Path"
    }
    return (Resolve-Path -LiteralPath $Path).Path
}

try {
    $resolvedImage = Resolve-RequiredFile -Path $ImagePath -Label "Flash image"
    $resolvedDll = Resolve-RequiredFile -Path $JLinkDllPath -Label "J-Link DLL"
    if ([string]::IsNullOrWhiteSpace($NrfjprogPath)) {
        $NrfjprogPath = "nrfjprog"
    }

    $tool = Get-Command -Name $NrfjprogPath -ErrorAction SilentlyContinue
    if ($null -eq $tool) {
        throw "nrfjprog not found: $NrfjprogPath"
    }
    $toolPath = if ($tool.Source) { $tool.Source } elseif ($tool.Path) { $tool.Path } else { $NrfjprogPath }

    $verifyEnabled = -not $NoVerify
    if (-not $PSBoundParameters.ContainsKey("NoVerify") -and $env:AXI_FLASH_VERIFY -eq "0") {
        $verifyEnabled = $false
    }

    $flashArgs = @("--program", $resolvedImage, "--chiperase", "--jdll", $resolvedDll)
    if ($verifyEnabled) {
        $flashArgs += "--verify"
    }
    $flashArgs += "--reset"
    if (-not [string]::IsNullOrWhiteSpace($ProbeId)) {
        $flashArgs += @("--snr", $ProbeId.Trim())
    }

    $sha256 = (Get-FileHash -LiteralPath $resolvedImage -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Host "[FLASH] Selected image: $resolvedImage"
    Write-Host "[FLASH] Image SHA256: $sha256"
    Write-Host "[FLASH] Command: $toolPath $($flashArgs -join ' ')"

    if ($DryRun) {
        Write-Host "[FLASH] Dry-run only; device was not programmed."
        exit 0
    }

    & $toolPath @flashArgs
    $exitCode = $LASTEXITCODE
    if ($null -eq $exitCode) {
        $exitCode = 0
    }
    exit $exitCode
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
