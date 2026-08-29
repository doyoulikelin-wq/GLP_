param(
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
$resolvedParent = [System.IO.Path]::GetFullPath((Split-Path -Parent $OutputDirectory))
if (-not (Test-Path $resolvedParent -PathType Container)) {
    throw "Output parent does not exist: $resolvedParent"
}
if (Test-Path $OutputDirectory) {
    throw "Refusing to overwrite existing output directory: $OutputDirectory"
}

New-Item -ItemType Directory -Path $OutputDirectory | Out-Null
$OutputDirectory = (Resolve-Path $OutputDirectory).Path
$script:FailureCodes = @()
$nativeExitCodePath = Join-Path $OutputDirectory "native_exit_codes.tsv"
"label`texit_code`tessential" |
    Set-Content -Encoding utf8 $nativeExitCodePath
$commandText = 'powershell -ExecutionPolicy Bypass -File "{0}" -OutputDirectory "{1}"' -f `
    $MyInvocation.MyCommand.Path, $OutputDirectory
$commandText | Set-Content -Encoding utf8 (Join-Path $OutputDirectory "command.txt")

function Add-FailureCode {
    param([string]$Code)
    if ($script:FailureCodes -notcontains $Code) {
        $script:FailureCodes += $Code
    }
}

function Invoke-NativeCapture {
    param(
        [Parameter(Mandatory = $true)][string]$CommandPath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$OutputFile,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][bool]$Essential
    )

    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $captured = & $CommandPath @Arguments 2>&1
        $nativeExitCode = $LASTEXITCODE
    } catch {
        $captured = $_ | Out-String
        $nativeExitCode = 9001
    } finally {
        $ErrorActionPreference = $savedPreference
    }
    $captured | Out-String |
        Set-Content -Encoding utf8 (Join-Path $OutputDirectory $OutputFile)
    "{0}`t{1}`t{2}" -f $Label, $nativeExitCode, $Essential.ToString().ToLowerInvariant() |
        Add-Content -Encoding utf8 $nativeExitCodePath
    if ($Essential -and $nativeExitCode -ne 0) {
        Add-FailureCode ("BLOCKED_{0}_EXIT_{1}" -f $Label.ToUpperInvariant(), $nativeExitCode)
    }
}

$startedAtUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
try {
    $startedAtUtc | Set-Content -Encoding utf8 (Join-Path $OutputDirectory "started_at_utc.txt")

    try {
        Get-ComputerInfo |
            Select-Object WindowsProductName, WindowsVersion, OsBuildNumber, `
                OsArchitecture, CsManufacturer, CsModel, CsProcessors, `
                CsTotalPhysicalMemory, BiosFirmwareType, TimeZone |
            ConvertTo-Json -Depth 4 |
        Set-Content -Encoding utf8 (Join-Path $OutputDirectory "windows_computer_info.json")
    } catch {
        ($_ | Out-String) |
            Set-Content -Encoding utf8 (Join-Path $OutputDirectory "windows_computer_info.error.txt")
        Add-FailureCode "BLOCKED_WINDOWS_COMPUTER_INFO"
    }

    try {
        Get-CimInstance Win32_VideoController |
            Select-Object Name, DriverVersion, AdapterRAM |
            ConvertTo-Json -Depth 3 |
            Set-Content -Encoding utf8 (Join-Path $OutputDirectory "windows_video_controllers.json")
    } catch {
        ($_ | Out-String) |
            Set-Content -Encoding utf8 (Join-Path $OutputDirectory "windows_video_controllers.error.txt")
        Add-FailureCode "BLOCKED_WINDOWS_VIDEO_CONTROLLER_QUERY"
    }

    try {
        $volumeInventory = Get-Volume |
            Select-Object DriveLetter, FileSystemLabel, FileSystem, DriveType, `
                HealthStatus, Size, SizeRemaining
        $volumeInventory |
            ConvertTo-Json -Depth 3 |
            Set-Content -Encoding utf8 (Join-Path $OutputDirectory "windows_volume_capacity.json")
        $eligibleVolume = $volumeInventory |
            Where-Object { $_.DriveType -eq "Fixed" -and $_.SizeRemaining -ge 80GB } |
            Select-Object -First 1
        if ($null -eq $eligibleVolume) {
            Add-FailureCode "BLOCKED_WINDOWS_FIXED_VOLUME_FREE_LT_80_GIB"
        }
    } catch {
        ($_ | Out-String) |
            Set-Content -Encoding utf8 (Join-Path $OutputDirectory "windows_volume_capacity.error.txt")
        Add-FailureCode "BLOCKED_WINDOWS_VOLUME_CAPACITY_QUERY"
    }

    $wslCommand = Get-Command wsl.exe -ErrorAction SilentlyContinue
    if ($null -eq $wslCommand) {
        "wsl.exe was not found" |
            Set-Content -Encoding utf8 (Join-Path $OutputDirectory "wsl_missing.txt")
        Add-FailureCode "BLOCKED_WSL_EXE_MISSING"
    } else {
        Invoke-NativeCapture -CommandPath $wslCommand.Source -Arguments @("--status") `
            -OutputFile "wsl_status.txt" -Label "WSL_STATUS" -Essential $true
        Invoke-NativeCapture -CommandPath $wslCommand.Source -Arguments @("--version") `
            -OutputFile "wsl_version.txt" -Label "WSL_VERSION" -Essential $true
        Invoke-NativeCapture -CommandPath $wslCommand.Source -Arguments @("--list", "--verbose") `
            -OutputFile "wsl_distributions.txt" -Label "WSL_LIST_VERBOSE" -Essential $true
    }

    $nvidiaCommand = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if ($null -eq $nvidiaCommand) {
        "nvidia-smi.exe was not found" |
            Set-Content -Encoding utf8 (Join-Path $OutputDirectory "nvidia_smi_missing.txt")
        Add-FailureCode "BLOCKED_NVIDIA_SMI_MISSING"
    } else {
        Invoke-NativeCapture -CommandPath $nvidiaCommand.Source -Arguments @(
            "--query-gpu=name,driver_version,memory.total",
            "--format=csv,noheader"
        ) -OutputFile "nvidia_smi_inventory.csv" -Label "NVIDIA_SMI_INVENTORY" -Essential $true
        Invoke-NativeCapture -CommandPath $nvidiaCommand.Source -Arguments @("-q") `
            -OutputFile "nvidia_smi_q.txt" -Label "NVIDIA_SMI_EXTENDED" -Essential $false
        Invoke-NativeCapture -CommandPath $nvidiaCommand.Source -Arguments @(
            "--query-gpu=temperature.gpu,power.limit",
            "--format=csv,noheader"
        ) -OutputFile "nvidia_smi_thermal_power.csv" -Label "NVIDIA_SMI_THERMAL_POWER" -Essential $false
    }
} catch {
    ($_ | Out-String) |
        Set-Content -Encoding utf8 (Join-Path $OutputDirectory "unhandled_failure.txt")
    Add-FailureCode "BLOCKED_UNHANDLED_WINDOWS_PROBE_ERROR"
}

$endedAtUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$endedAtUtc | Set-Content -Encoding utf8 (Join-Path $OutputDirectory "ended_at_utc.txt")
$failureCodesText = $script:FailureCodes -join "`r`n"
$failureCodesText | Set-Content -Encoding utf8 (Join-Path $OutputDirectory "failure_codes.txt")

if ($script:FailureCodes.Count -eq 0) {
    $status = "WINDOWS_HOST_PROBE_PASS_NOT_G1"
    $exitCode = 0
} else {
    $status = "WINDOWS_HOST_PROBE_FAIL"
    $exitCode = 65
}
$status | Set-Content -Encoding utf8 (Join-Path $OutputDirectory "STATUS.txt")
$exitCode | Set-Content -Encoding utf8 (Join-Path $OutputDirectory "exit_code.txt")

$evidenceManifest = Join-Path $OutputDirectory "EVIDENCE.SHA256.tsv"
$evidenceRows = Get-ChildItem -File $OutputDirectory |
    Where-Object { $_.Name -notin @("EVIDENCE.SHA256.tsv", "receipt.json", "RECEIPT.SHA256SUMS") } |
    Sort-Object Name |
    ForEach-Object {
        $hash = Get-FileHash -Algorithm SHA256 $_.FullName
        "{0}`t{1}`t{2}" -f $hash.Hash.ToLowerInvariant(), $_.Length, $_.Name
    }
@("sha256`tbytes`tname") + @($evidenceRows) |
    Set-Content -Encoding utf8 $evidenceManifest

$evidenceManifestHash = (Get-FileHash -Algorithm SHA256 $evidenceManifest).Hash.ToLowerInvariant()
$receipt = [ordered]@{
    schema_version = "WINDOWS_HOST_ENGINEERING_PROBE_RECEIPT_V1"
    status = $status
    exit_code = $exitCode
    formal_g1 = $false
    started_at_utc = $startedAtUtc
    ended_at_utc = $endedAtUtc
    failure_codes = @($script:FailureCodes)
    evidence_manifest_sha256 = $evidenceManifestHash
}
$receiptPath = Join-Path $OutputDirectory "receipt.json"
$receipt | ConvertTo-Json -Depth 5 |
    Set-Content -Encoding utf8 $receiptPath

@($evidenceManifest, $receiptPath) | ForEach-Object {
    $hash = Get-FileHash -Algorithm SHA256 $_
    "{0}`t{1}`t{2}" -f $hash.Hash.ToLowerInvariant(), $_.Length, (Split-Path -Leaf $_)
} | Set-Content -Encoding utf8 (Join-Path $OutputDirectory "RECEIPT.SHA256SUMS")

Write-Output ("{0}: {1}" -f $status, $OutputDirectory)
exit $exitCode
