# ============================================================================
#  九账算术 · 本地启动（模拟替身模式，不接真模型）
# ============================================================================
#  用途：不需要 API key，也不需要联网调模型 —— 意图判定走规则，
#        适合"只想看界面和数据"或现场断网演示。
#
#  用法：.\scripts\serve_local.ps1
#  停止：Ctrl + C
# ============================================================================

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Host "[local] 找不到虚拟环境，请先在项目根目录运行 .\deploy.ps1" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}

# 演示库必须存在：否则服务能起来，但每次查询都报错，看起来像"功能坏了"
$Db = Join-Path $Root "app" "data" "banking_sim.db"
if (-not (Test-Path $Db)) {
    Write-Host "[local] 找不到演示数据库，正在自动重建..." -ForegroundColor Yellow
    & $Py -m app.db.init_db --reset
    if (-not (Test-Path $Db)) {
        Write-Host "[local] 重建失败，请运行 .\deploy.ps1" -ForegroundColor Red
        Read-Host "按回车退出"
        exit 1
    }
}

# 端口占用：uvicorn 只会报 [WinError 10048]，不直观，提前查并给处置命令
$busy = $false
try {
    $tc = New-Object Net.Sockets.TcpClient
    $tc.Connect("127.0.0.1", 8000)
    $busy = $tc.Connected
    $tc.Close()
} catch { $busy = $false }
if ($busy) {
    Write-Host "[local] 端口 8000 已被占用（多半是上一次的服务还开着）" -ForegroundColor Red
    Write-Host '        处置：Get-NetTCPConnection -LocalPort 8000 | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }'
    Read-Host "按回车退出"
    exit 1
}

# 清掉可能残留的 key，确保走替身模式（不静默继承）
Remove-Item Env:\MODEL_API_KEY -ErrorAction SilentlyContinue

Write-Host "[local] 启动后端 8000（模拟替身模式）" -ForegroundColor Cyan
Write-Host "[local] 网页前端：http://localhost:8000/ui" -ForegroundColor Cyan
Write-Host "[local] 按 Ctrl + C 停止" -ForegroundColor DarkGray
Write-Host ""

& $Py -m uvicorn app.main:app --host 127.0.0.1 --port 8000
