#!/usr/bin/env bash
# 打包交付包（2026-10-22 新增）
#
# 为什么需要这个脚本：此前手工敲 zip 命令，漏排除了 `.env`，导致占位用 .env 被带进交付包。
# 本次虽只是占位文本、无实际泄露，但只要队友填入真实 key 后再手工打包，密钥就会随包流出。
# 把排除规则固化进脚本，避免依赖记忆。
#
# 用法（项目根目录执行）：
#   bash scripts/pack.sh                    # 产出 ../九账算术_交付包_YYYY-MM-DD.zip
#   bash scripts/pack.sh /tmp/custom.zip    # 指定输出路径
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="$(basename "$ROOT")"
OUT="${1:-$(dirname "$ROOT")/${NAME}_交付包_$(date +%Y-%m-%d).zip}"

cd "$(dirname "$ROOT")"

echo "[pack] 源目录: $ROOT"
echo "[pack] 输出:   $OUT"

# 排除清单（任何一条都不要删）：
#   .venv / __pycache__ / *.pyc  → 环境与缓存，体积大且平台相关
#   .git                         → 版本史，非交付物
#   .env / .env.local / config.yaml → 【密钥】绝不可入包，只发 .env.example
#   *.db / *.db-wal / *.db-shm   → 演示库由 init_db --reset 生成，入包会带来过期数据
#   *.log / .DS_Store / Thumbs.db → 运行日志与系统垃圾
rm -f "$OUT"
zip -rq "$OUT" "$NAME" \
  -x "${NAME}/.venv/*" \
  -x "*/__pycache__/*" \
  -x "*.pyc" \
  -x "${NAME}/.git/*" \
  -x "${NAME}/.env" \
  -x "*/env.local" \
  -x "${NAME}/.env.local" \
  -x "${NAME}/config.yaml" \
  -x "*/config.yaml" \
  -x "*/banking_sim.db" \
  -x "*.db-journal" \
  -x "*.db-wal" \
  -x "*.db-shm" \
  -x "*.log" \
  -x "*.DS_Store" \
  -x "*/Thumbs.db"

# ---- 打包后自检：这几类东西一旦出现就立即失败，不让带病包流出去 ----
# 注意（踩坑记录 2026-10-22）：不要写成 `unzip -l | grep -q`。
#   在 `set -o pipefail` 下，grep -q 一旦命中就立即退出，unzip 仍在写管道会收到 SIGPIPE
#   而返回非零，pipefail 据此把整条管道判为失败，「命中」反而走进 else 分支 —— 自检静默失效。
#   故障注入实测确认过这个问题，所以改为先落变量再匹配。
FAIL=0
LISTING="$(unzip -l "$OUT")"
check_absent() {
  local pattern="$1" label="$2"
  local hits
  hits="$(printf '%s\n' "$LISTING" | grep -E "$pattern" || true)"
  if [ -n "$hits" ]; then
    echo "[pack][FAIL] 交付包含有${label}："
    printf '%s\n' "$hits" | head -3 | sed 's/^/           /'
    FAIL=1
  fi
}
check_absent '(^|/)\.env$'                 '密钥文件 .env'
check_absent '(^|/)config\.yaml$'          '本地配置 config.yaml'
check_absent '\.venv/'                     '虚拟环境'
check_absent '__pycache__'                 'Python 缓存'
check_absent '\.pyc$'                      'pyc 字节码'
check_absent '(^|/)\.git/'                 '版本库'
check_absent 'banking_sim\.db'             '演示数据库'

if [ "$FAIL" -ne 0 ]; then
  echo "[pack] 自检未通过，已删除产物，请修正排除规则后重试"
  rm -f "$OUT"
  exit 1
fi

echo "[pack] 自检通过：无密钥/配置/环境/缓存/数据库"
echo "[pack] 文件数: $(unzip -l "$OUT" | tail -1 | awk '{print $2}')  大小: $(du -h "$OUT" | cut -f1)"
echo "[pack] 完成: $OUT"
