# 用真实 DeepSeek 模型启动（Windows PowerShell 版）
#
# 与 scripts/serve_real.sh 等价，只是换成 PowerShell 语法。
# 用法（在解压出来的「九账算术」目录里执行）：
#   .\scripts\serve_real.ps1 -Key sk-你的key
#
# 若提示"禁止运行脚本"，先执行一次（仅当前用户）：
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#
# 停止：Ctrl + C

param(
    [Parameter(Mandatory = $true)]
    [string]$Key
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Log($m) { Write-Host "[real] $m" }

# ---- 1. 校验 key ----
if (-not $Key.StartsWith("sk-")) {
    Write-Host "[real] 警告：key 通常以 sk- 开头，当前不是。仍继续尝试。" -ForegroundColor Yellow
}
if ($Key.Length -lt 20) {
    Write-Host "[real] 警告：key 长度只有 $($Key.Length)，似乎不完整。" -ForegroundColor Yellow
}

# ---- 2. 找解释器 ----
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Log "找不到虚拟环境：$Py"
    Log "请先在项目根目录执行："
    Write-Host "    python -m venv .venv"
    Write-Host "    .\.venv\Scripts\Activate.ps1"
    Write-Host "    pip install -r requirements.txt"
    Write-Host "    python -m app.db.init_db --reset"
    exit 1
}

# ---- 3. 占位纪律：只在本进程内设变量，不写文件、不改用户环境 ----
# 注意：不写进 .env，也不 setx。作用域仅限本脚本及其子进程，
#       脚本结束即消失；不会污染后续 serve_local.ps1 的替身模式。
$env:MODEL_PROVIDER = "deepseek"
$env:MODEL_NAME     = "deepseek-flash"
$env:MODEL_API_BASE = "https://api.deepseek.com/v1"
$env:MODEL_API_KEY  = $Key

# ---- 4. 起后端前先验证 key 真的能用 ----
# 纪律：`配置了 key` ≠ `key 有效`。只检查"有没有填"就打绿灯，是假信号。
#       这里真的发一次意图分类请求（走项目自己的 llm 模块，与后端同一条路径），
#       401/402/超时立刻暴露，不等到演示现场才发现。
#       注意：结果只打印状态与原因，不回显 key 本身。
$ErrorActionPreference = "Continue"   # 理由见 §6
Log "验证 key（会真实调用一次模型，约 1-3 秒）"
$probeCode = 'from app.planning.llm import classify_intent; r = classify_intent("这个月哪笔花得不对劲", timeout=20); print("PROBE_OK " + str(r.intent) if r.ok else "PROBE_FAIL " + str(r.reason or "unknown"))'
$probeOut = (& $Py -c $probeCode 2>&1 | Out-String).Trim()

if ($probeOut -match "PROBE_OK\s*(\S*)") {
    Log "key 可用，模型已连通（意图识别 $($Matches[1])）"
} else {
    Write-Host ""
    Write-Host "[real] ============================================" -ForegroundColor Red
    Write-Host "[real]  key 验证失败，后端未启动" -ForegroundColor Red
    Write-Host "[real] ============================================" -ForegroundColor Red
    if ($probeOut -match "PROBE_FAIL (\S+)") {
        $reason = $Matches[1]
        Write-Host "[real]  原因代码：$reason" -ForegroundColor Red
        if ($reason -like "http_401*" -or $reason -like "*auth*") {
            Write-Host "[real]  -> key 无效或已过期，检查是否复制完整（没有多余空格、没有漏掉尾部字符）"
            Write-Host "[real]     DeepSeek 控制台：https://platform.deepseek.com/api_keys"
        } elseif ($reason -like "http_402*" -or $reason -like "*insufficient*") {
            Write-Host "[real]  -> 账户余额不足，请先充值"
        } elseif ($reason -eq "timeout") {
            Write-Host "[real]  -> 请求超时：网络不通，或该区域访问受限"
        } elseif ($reason -eq "not_configured") {
            Write-Host "[real]  -> 没读到配置，确认 -Key 参数已传入"
        } else {
            Write-Host "[real]  -> 未归类的失败原因，把上面几行发给助手"
        }
    } else {
        Write-Host "[real]  探测输出无法识别，原文如下：" -ForegroundColor Yellow
        Write-Host $probeOut -ForegroundColor DarkGray
    }
    Write-Host ""
    Write-Host "[real]  想先看界面（不调模型）：.\scripts\serve_local.ps1" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "按回车退出"
    exit 1
}

# ---- 5. 端口占用检查 ----
# uvicorn 端口被占时会报 [WinError 10048]，信息不直观；提前查并给处置命令。
$busy = $false
try {
    $tc = New-Object Net.Sockets.TcpClient
    $tc.Connect("127.0.0.1", 8000)
    $busy = $tc.Connected
    $tc.Close()
} catch { $busy = $false }
if ($busy) {
    Write-Host "[real] 端口 8000 已被占用（多半是上一次的服务还开着）" -ForegroundColor Red
    Write-Host "[real] 处置：关掉那个窗口，或执行下面这条强制结束占用进程" -ForegroundColor Yellow
    Write-Host '       Get-NetTCPConnection -LocalPort 8000 | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }'
    Read-Host "按回车退出"
    exit 1
}

# ---- 6. 起后端（前台运行，方便看日志） ----
# 注意：这里把 ErrorActionPreference 设为 Continue。
#   Windows PowerShell 5.1 在 Stop 下会把原生命令写到 stderr 的内容当成终止性错误
#   （NativeCommandError），而 uvicorn 的日志默认全部走 stderr —— 服务会"启动即退出"，
#   且报错信息看不出真因。这是踩过的坑，别改回 Stop。
$ErrorActionPreference = "Continue"
Log "启动后端 8000（真模型模式）"
Log "网页前端：http://localhost:8000/ui"
Log "按 Ctrl + C 停止"
Write-Host ""
& $Py -m uvicorn app.main:app --host 127.0.0.1 --port 8000
