# ============================================================================
#  九账算术 · 一键部署（Windows PowerShell）
# ============================================================================
#  作用：把这个目录里的项目「安装到可以直接运行」的状态。
#        包含：检查 Python → 建虚拟环境 → 装依赖 → 建演示数据库 → 自检
#        不包含：填 API key（这一步故意留给你本人，脚本不碰密钥）
#
#  用法（Windows 11 已移除「右键 → 使用 PowerShell 运行」，用下面这种方式）：
#    1. 用文件资源管理器打开本文件所在的文件夹（能看到 app、frontend 的这一层）
#    2. 点击窗口顶部的地址栏，删掉路径，输入 powershell 后回车
#       （这样打开的 PowerShell，工作目录已经在这里，不用再 cd）
#    3. 执行：
#          powershell -ExecutionPolicy Bypass -File .\deploy.ps1
#
#  说明：-ExecutionPolicy Bypass 只对这一次执行生效，不改你的系统设置。
#        如果你已经执行过 Set-ExecutionPolicy -Scope CurrentUser RemoteSigned，
#        直接 .\deploy.ps1 也可以。
# ============================================================================

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Step($n, $t) { Write-Host ""; Write-Host "[$n] $t" -ForegroundColor Cyan }
function Ok($m)       { Write-Host "    OK  $m" -ForegroundColor Green }
function Warn($m)     { Write-Host "    注意  $m" -ForegroundColor Yellow }
function Fail($m)     { Write-Host "    失败  $m" -ForegroundColor Red }
function Info($m)     { Write-Host "          $m" -ForegroundColor DarkGray }

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  九账算术 · 部署到本机" -ForegroundColor Cyan
Write-Host "  目标目录：$Root" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# ---------------------------------------------------------------- 1. Python
Step 1 "检查 Python"
$PyLauncher = $null
foreach ($cand in @("python", "python3", "py")) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) {
        $ver = & $cand --version 2>&1
        if ($ver -match "Python (\d+)\.(\d+)") {
            $maj = [int]$Matches[1]; $min = [int]$Matches[2]
            Info "$cand -> $ver"
            if ($maj -eq 3 -and $min -ge 10) { $PyLauncher = $cand; break }
            else { Warn "$ver 版本偏低（需 3.10+），继续找" }
        }
    }
}
if (-not $PyLauncher) {
    Fail "没找到 Python 3.10 或更高版本"
    Info "请到 https://www.python.org/downloads/ 安装，安装时务必勾选 Add python.exe to PATH"
    Read-Host "按回车退出"
    exit 1
}
Ok "使用 $PyLauncher"

# ------------------------------------------------------- 2. 虚拟环境
Step 2 "创建虚拟环境 .venv"
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path $VenvPy) {
    Ok "已存在，跳过创建"
} else {
    Info "正在创建（约 10 秒）..."
    & $PyLauncher -m venv .venv
    if (-not (Test-Path $VenvPy)) {
        Fail "虚拟环境创建失败"
        Read-Host "按回车退出"
        exit 1
    }
    Ok "创建完成"
}

# ------------------------------------------------------- 3. 安装依赖
Step 3 "安装依赖"
Info "用国内镜像（清华源）加速，约 1-3 分钟，请耐心等"
& $VenvPy -m pip install --upgrade pip -q --disable-pip-version-check 2>&1 | Out-Null
$mirror = "https://pypi.tuna.tsinghua.edu.cn/simple"
& $VenvPy -m pip install -r requirements.txt -i $mirror --disable-pip-version-check
if ($LASTEXITCODE -ne 0) {
    Warn "镜像安装失败，改用官方源重试"
    & $VenvPy -m pip install -r requirements.txt --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) {
        Fail "依赖安装失败，请把上面的红色报错发给 AI 助手"
        Read-Host "按回车退出"
        exit 1
    }
}
Ok "依赖安装完成"

# ------------------------------------------------------- 4. 建数据库
Step 4 "初始化演示数据库"
Info "生成 6 个月模拟流水（2026-04 至 2026-09）"
& $VenvPy -m app.db.init_db --reset
if ($LASTEXITCODE -ne 0) {
    Fail "建库失败"
    Read-Host "按回车退出"
    exit 1
}
Ok "数据库就绪"

# ------------------------------------------------------- 5. 自检
Step 5 "自检（不联网、不调模型）"
# 自检逻辑放在 scripts/selfcheck.py，不写在 PowerShell 里。
# 原因：PowerShell 的 @"..."@ here-string 对缩进和引号极敏感，一旦手抖就整段
#       语法错，而报错位置往往指向无关行，排查成本很高。独立成 .py 后，
#       PowerShell 端只剩「一行调用 + 看退出码」。
$SelfCheck = Join-Path $Root "scripts" "selfcheck.py"
& $VenvPy $SelfCheck
$SelfCheckOk = ($LASTEXITCODE -eq 0)
if (-not $SelfCheckOk) {
    Warn "自检有失败项（见上方列表）。这不是正常状态，请把上面的输出发给助手排查"
}

# ------------------------------------------------------- 6. 收尾
# 纪律：不能出现「自检没过却打印成功」。两种情况分别给不同标题和颜色。
Write-Host ""
if ($SelfCheckOk) {
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "  部署完成，自检全部通过" -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
} else {
    Write-Host "============================================================" -ForegroundColor Yellow
    Write-Host "  环境已装好，但自检有失败项 —— 请先看上方 [5] 的输出" -ForegroundColor Yellow
    Write-Host "============================================================" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "  下一步（填 API key，由你本人操作）：" -ForegroundColor White
Write-Host ""
Write-Host "    .\scripts\serve_real.ps1 -Key sk-你的key" -ForegroundColor Yellow
Write-Host ""
Write-Host "  然后浏览器打开： http://localhost:8000/ui" -ForegroundColor White
Write-Host ""
Write-Host "  不想用真模型（纯演示，零网络）：" -ForegroundColor White
Write-Host "    .\scripts\serve_local.ps1" -ForegroundColor DarkGray
Write-Host ""
Read-Host "按回车关闭本窗口"
