# 设置 Windows Task Scheduler 定时任务
# 需要管理员权限运行

param(
    [string]$Time = "15:00",  # 每日执行时间 (15:00 = 下午3点，收盘后)
    [string]$TaskName = "QuantSignal"
)

# 获取项目根目录
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
$scriptPath = Join-Path $scriptDir "run_signal_service.ps1"

# 检查管理员权限
$isAdmin = [Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent() | `
    % { $_.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) }

if (-not $isAdmin) {
    Write-Host "❌ 此脚本需要管理员权限运行" -ForegroundColor Red
    Write-Host "请右键选择 'Run as Administrator' 后重新执行" -ForegroundColor Yellow
    exit 1
}

# 删除已存在的任务
Write-Host "检查现有任务..." -ForegroundColor Cyan
try {
    schtasks /Query /TN $TaskName > $null 2>&1
    if ($?) {
        Write-Host "删除现有任务 '$TaskName'..." -ForegroundColor Yellow
        schtasks /Delete /TN $TaskName /F | Out-Null
        Start-Sleep -Seconds 1
    }
}
catch {}

# 创建新任务
Write-Host "创建每日定时任务..." -ForegroundColor Cyan
$taskDescription = "量化信号播报服务 - 自动拉取数据并推送飞书"
$taskCommand = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""

try {
    schtasks /Create `
        /SC DAILY `
        /ST $Time `
        /TN $TaskName `
        /TR $taskCommand `
        /F | Out-Null

    Write-Host "✅ 任务创建成功!" -ForegroundColor Green
    Write-Host ""
    Write-Host "任务信息:" -ForegroundColor Cyan
    Write-Host "  名称:    $TaskName"
    Write-Host "  时间:    每日 $Time"
    Write-Host "  脚本:    $scriptPath"
    Write-Host ""
    Write-Host "后续操作:" -ForegroundColor Yellow
    Write-Host "  查看任务:   schtasks /Query /TN $TaskName /V"
    Write-Host "  立即运行:   schtasks /Run /TN $TaskName"
    Write-Host "  查看日志:   Get-Content $projectRoot\logs\signal_service.log -Tail 50"
    Write-Host "  删除任务:   schtasks /Delete /TN $TaskName /F"
    Write-Host ""
}
catch {
    Write-Host "❌ 任务创建失败: $_" -ForegroundColor Red
    exit 1
}
