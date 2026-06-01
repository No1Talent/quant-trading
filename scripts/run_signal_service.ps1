# Signal Service 自动化脚本
# 每日定时执行：拉取最新数据 → 生成信号 → 推送飞书
# 通过 Windows Task Scheduler 调用
#
# 创建任务（管理员 PowerShell）：
#   schtasks /Create /SC DAILY /ST 15:00 /TN "QuantSignal" /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Quant\scripts\run_signal_service.ps1"
#
# 查看任务：
#   schtasks /Query /TN "QuantSignal" /V
#
# 删除任务：
#   schtasks /Delete /TN "QuantSignal" /F

# 配置
$scriptRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$logDir = Join-Path $scriptRoot "logs"
$logFile = Join-Path $logDir "signal_service.log"

# 创建日志目录
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

# 开始执行日志
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Write-Output "[$timestamp] Starting signal service with auto-refresh..." | Tee-Object -Append $logFile

# 激活虚拟环境（如果存在）
$venvPath = Join-Path $scriptRoot "venv\Scripts\Activate.ps1"
if (Test-Path $venvPath) {
    & $venvPath
}

# 切换到项目根目录并执行
try {
    Set-Location $scriptRoot
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Output "[$timestamp] Running signal_service.py --auto-refresh --only-on-signal..." | Tee-Object -Append $logFile

    python signal_service.py --auto-refresh --only-on-signal 2>&1 | Tee-Object -Append $logFile
    $exitCode = $LASTEXITCODE

    if ($exitCode -ne 0) {
        $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Write-Output "[$timestamp] ❌ Signal service failed with exit code $exitCode" | Tee-Object -Append $logFile
        exit $exitCode
    }

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Output "[$timestamp] ✅ Signal service completed successfully" | Tee-Object -Append $logFile
}
catch {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Output "[$timestamp] ❌ Exception: $_" | Tee-Object -Append $logFile
    exit 1
}
