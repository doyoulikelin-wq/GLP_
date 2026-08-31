param(
    [string]$Distribution = "Ubuntu-24.04"
)

$ErrorActionPreference = "Stop"

$overlayRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

try {
    $null = & wsl.exe -d $Distribution -- true 2>&1
} catch {
    Write-Host "未检测到 Ubuntu 24.04 WSL2。请先在管理员 PowerShell 执行："
    Write-Host "wsl --install -d Ubuntu-24.04"
    exit 65
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "Ubuntu 24.04 WSL2 尚未准备好。请先执行并按提示重启 Windows："
    Write-Host "wsl --install -d Ubuntu-24.04"
    exit 65
}

$rawWslPath = & wsl.exe -d $Distribution -- wslpath -a $overlayRoot 2>&1
$wslPathExitCode = $LASTEXITCODE
if ($wslPathExitCode -ne 0 -or $null -eq $rawWslPath) {
    Write-Host "无法把 Windows 附加包路径转换为 WSL2 路径：$overlayRoot"
    exit 66
}
$wslOverlayRoot = ($rawWslPath | Out-String).Trim()
if ([string]::IsNullOrWhiteSpace($wslOverlayRoot)) {
    Write-Host "WSL2 返回了空的附加包路径：$overlayRoot"
    exit 66
}

Write-Host "开始既有权重 VHH 推理、候选生成和筛选。"
Write-Host "第一次运行会安装依赖；若 Ubuntu 询问密码，请输入 Ubuntu 用户密码。"

& wsl.exe -d $Distribution -- bash "$wslOverlayRoot/scripts/wsl/start_personal_vhh_inference.sh"
$runExitCode = $LASTEXITCODE

if ($runExitCode -eq 0) {
    Write-Host "完成。请查看 Ubuntu 中 ~/boltzgen_personal/logs/ 下的最新日志。"
} else {
    Write-Host "运行未完成，退出码：$runExitCode。日志保留在 Ubuntu 的 ~/boltzgen_personal/logs/。"
}

exit $runExitCode
