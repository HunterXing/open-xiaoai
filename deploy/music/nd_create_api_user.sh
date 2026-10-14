#!/bin/bash
# 给 Navidrome 建专用 API 用户（播放用，非管理员）。
#
# 为什么要用脚本：navidrome user create 必须在**真 TTY** 里跑，
# docker exec 不带 -it 会失败；而 TTY 会把输入的口令回显出来，
# 所以这里用 script(1) 造一个伪 TTY，并把输出按**值**打码后才允许上终端
#（按行/按提示符打码都不行——TTY 会把口令拆在很多行里回显）。
#
# 口令由本脚本随机生成，全程只在 600 权限文件与管道里流转；脚本最后
# 打印口令文件路径，自己 cat 一次填进 .env 即可（别贴进聊天窗口/工单）。
#
# 为什么不能跳过 CLI 直接写库：Navidrome 的 user 表里口令是**可逆加密**
# 存储（Subsonic token 鉴权需要明文校验），不是 bcrypt，写哈希进不去。
#
# 用法：
#   ./nd_create_api_user.sh xiaozhi
#   ND_CONTAINER=navidrome ND_DB=/opt/navidrome/data/navidrome.db ./nd_create_api_user.sh xiaozhi
set -uo pipefail

UNAME="${1:-xiaozhi}"
ND_CONTAINER="${ND_CONTAINER:-navidrome}"
ND_DB="${ND_DB:-/opt/navidrome/data/navidrome.db}"

PW_FILE=$(mktemp "${TMPDIR:-/tmp}/nd-pw-XXXXXX"); chmod 600 "$PW_FILE"
IN_FILE=$(mktemp "${TMPDIR:-/tmp}/nd-in-XXXXXX"); chmod 600 "$IN_FILE"
LOG=$(mktemp "${TMPDIR:-/tmp}/nd-log-XXXXXX");    chmod 600 "$LOG"

head -c 96 /dev/urandom | base64 | tr -d '/+=\n' | cut -c1-24 > "$PW_FILE"
PW=$(cat "$PW_FILE")
{ printf '%s\n' "$PW"; printf '%s\n' "$PW"; } > "$IN_FILE"

# mask(): 把口令按值替换掉再输出——TTY 会回显输入，不能靠行匹配打码
mask() { sed "s|$PW|<REDACTED>|g" "$1"; }

echo "== 创建用户 $UNAME（容器内需要真 TTY，故 -it + script 双保险）=="
script -qec "docker exec -it $ND_CONTAINER /app/navidrome user create -u $UNAME --name 'Voice assistant'" \
    /dev/null < "$IN_FILE" > "$LOG" 2>&1
echo "exit=$?"
mask "$LOG" | grep -aoE 'level=[a-z]+ msg="[^"]*"|successfully|User created' | tail -3

echo "== 用户表 =="
sqlite3 "$ND_DB" "select user_name, name, is_admin from user;"

if sqlite3 "$ND_DB" "select 1 from user where user_name='$UNAME';" | grep -q 1; then
    chmod 600 "$PW_FILE"; rm -f "$IN_FILE" "$LOG"
    echo "PW_FILE=$PW_FILE"
else
    echo "创建失败；完整日志（已打码）见 $LOG"
    mask "$LOG" | tail -6
fi
