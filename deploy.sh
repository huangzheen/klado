#!/usr/bin/env bash
# Klado — 一键自部署。
#
# 两种用法，脚本自己判断：
#   1. 已经有代码：   git clone … && cd klado && ./deploy.sh you@example.com
#   2. 什么都没有：   curl -fsSL https://raw.githubusercontent.com/huangzheen/klado/main/deploy.sh | ADMIN_EMAIL=you@example.com bash
#
# 第二个用法能成立，是因为本脚本在「不在 Klado 检出目录里」时会自己 clone。
#
# ── 为什么需要这个脚本，而不是直接 `docker compose up -d` ──────────────────
# 照 .env.example 原样复制再 up，会以三种方式失败，其中两种不会报错：
#
#   1. compose 有三个必填变量：POSTGRES_PASSWORD、OSS_ACCESS_KEY_ID、
#      OSS_ACCESS_KEY_SECRET。缺任何一个，`docker compose` 直接拒绝启动。
#      —— 这个至少会报错。
#   2. AUTH_ADMIN_EMAILS 留空。而 `AUTH_ENABLED=false` 时的单用户模式需要它：
#      空值意味着没有隐含身份，于是**每一个 /api 请求都返回 401**。容器起来了、
#      健康检查是绿的、首页能打开，只是所有操作都失败。不看日志根本发现不了。
#   3. SECRET_KEY 留着 change-me。不会报错，只是所有会话共用一个公开的密钥。
#
# 三种都由本脚本填掉。
set -euo pipefail

REPO_URL="${KLADO_REPO_URL:-https://github.com/huangzheen/klado.git}"
BRANCH="${KLADO_BRANCH:-main}"
DIR="${KLADO_DIR:-$PWD/klado}"
APP_PORT="${KLADO_APP_PORT:-8000}"
ADMIN_PORT="${KLADO_ADMIN_PORT:-8787}"
# ⚠️ 默认只绑回环。新装的一台机器在局域网里裸奔、且 AUTH_ENABLED=false
#    （没登录 anybody 就是管理员）是一个很容易发生的严重配置错误。
#    要对外暴露请显式写 KLADO_BIND=0.0.0.0，并且先把 AUTH_ENABLED 打开。
BIND="${KLADO_BIND:-127.0.0.1}"

say()  { printf '\033[1;34m▸\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

random() { python3 -c "import secrets;print(secrets.token_urlsafe(32))"; }

# ── 0. 预检 ────────────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || die "找不到 docker。先装 Docker。"
docker compose version >/dev/null 2>&1 || die "找不到 docker compose v2（docker-compose 老版本不行）。"
docker info >/dev/null 2>&1 || die "docker daemon 没在跑，或者当前用户不在 docker 组里。"
command -v python3 >/dev/null 2>&1 || die "找不到 python3（用来生成随机密钥）。"

# ── 1. 拿到代码 ────────────────────────────────────────────────────────────
# 在检出目录里就直接用；否则 clone 到 $DIR。判断依据是这两个文件同时存在，
# 而不是「脚本路径」——后者在 `curl | bash` 时是 /dev/stdin，没有参考价值。
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || echo "")"
if [[ -f "$SELF_DIR/Dockerfile" && -f "$SELF_DIR/docker-compose.yml" ]]; then
  DIR="$SELF_DIR"
  say "使用已有检出目录 $DIR"
elif [[ -f "./docker-compose.yml" && -f "./deploy.sh" ]]; then
  DIR="$PWD"
  say "使用当前目录 $DIR"
else
  say "克隆 $REPO_URL → $DIR"
  [[ -e "$DIR" ]] && die "$DIR 已存在但不是 Klado 目录。换一个 KLADO_DIR。"
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$DIR"
fi
cd "$DIR"

# ── 2. 管理员身份 ──────────────────────────────────────────────────────────
# 没有默认邮箱：猜一个会把别人的部署变成别人的。管道进来的脚本没有 TTY，
# 也不能靠交互提示，所以缺失时明确报出正确的写法，而不是静默挑一个。
ADMIN_EMAIL="${ADMIN_EMAIL:-${1:-}}"
if [[ -z "$ADMIN_EMAIL" ]]; then
  [[ -t 0 ]] || die "缺少管理员邮箱。
    管道执行没有交互提示，请这样写：
      ADMIN_EMAIL=you@example.com bash
    或先 clone 再执行：
      git clone $REPO_URL && cd klado && ./deploy.sh you@example.com"
  read -r -p "管理员邮箱（以后用它登录）: " ADMIN_EMAIL
fi
[[ "$ADMIN_EMAIL" == *@*.* ]] || die "邮箱看起来不合法：$ADMIN_EMAIL"

# ── 3. 写 .env ─────────────────────────────────────────────────────────────
# 幂等：已经存在的 .env 一律不动，只补缺失的行。重复跑 deploy.sh 不应该
# 换掉 SECRET_KEY——那会让所有已登录会话失效，也会让已存的 SMTP 密码解不开。
verify_secrets() {
  local key val missing=()
  # compose 里带 `:?` 的三个必填项，加上 SECRET_KEY（空或占位值等于公开密钥）。
  for key in SECRET_KEY POSTGRES_PASSWORD OSS_ACCESS_KEY_ID OSS_ACCESS_KEY_SECRET; do
    val="$(grep -E "^${key}=" .env | head -1 | cut -d= -f2- || true)"
    if [[ -z "$val" || "$val" == change-me* ]]; then missing+=("$key"); fi
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    die ".env 里这些值是空的或还是占位值：${missing[*]}
  前三项 compose 会直接拒绝启动；SECRET_KEY 留成 change-me 则所有会话共用一个公开密钥。
  改好 .env 里这几行，再跑一次本脚本。"
  fi

  # ⚠️ AUTH_ADMIN_EMAILS 只有在**没开认证**时才是必需的 —— 开了 AUTH_ENABLED
  #    之后管理员由应用自己管，这一行可以合法地留空。所以不能无条件要求它，
  #    那会把一个正确的配置判成坏的；而在单用户模式下留空，它就是致命的：
  #    容器健康、首页打得开，但每个 /api 请求都是 401。
  local auth_enabled admins
  auth_enabled="$(grep -E '^AUTH_ENABLED=' .env | head -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
  admins="$(grep -E '^AUTH_ADMIN_EMAILS=' .env | head -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
  if [[ "$auth_enabled" != "true" && "$auth_enabled" != "1" && -z "$admins" ]]; then
    die ".env 里 AUTH_ADMIN_EMAILS 是空的，而 AUTH_ENABLED 没有打开。
  这种配置下没有隐含身份，每一个 /api 请求都会返回 401 —— 容器是健康的、首页能打开，
  但什么都做不了，而且日志里也不会有报错。填一个邮箱，或把 AUTH_ENABLED 改成 true。"
  fi
}

if [[ -f .env ]]; then
  say "发现已有 .env，保留它（不改动任何已配置的值）"
else
  say "生成 .env"
  cp .env.example .env
  # set_env KEY VALUE —— 只有 KEY 当前是空值或占位值时才写。
  #
  # ⚠️ 这里不用 `grep -E "^KEY=(|change-me)"`：空分支在 BSD grep（macOS 自带）
  #    直接报 "empty (sub)expression" 并且**不匹配任何东西**。于是一个变量都没
  #    被写入，而脚本照样往下走、照样打印"已写入随机密钥"、照样报告部署成功——
  #    而 .env 里留着公开的 SECRET_KEY 和空的 OSS 密钥。所以取值用字符串比较，
  #    不把「空」写进正则；写完还有一道 verify_secrets 兜底。
  set_env() {
    local key="$1" val="$2" existing
    existing="$(grep -E "^${key}=" .env | head -1 | cut -d= -f2- || true)"
    if [[ -n "$existing" && "$existing" != change-me* ]]; then
      return 0
    fi
    local tmp; tmp="$(mktemp)"
    awk -v k="$key" -v v="$val" 'BEGIN{FS=OFS="="}
      $1==k {print k"="v; next} {print}' .env > "$tmp"
    mv "$tmp" .env
  }
  set_env SECRET_KEY          "$(random)"
  set_env POSTGRES_PASSWORD   "$(random)"
  set_env OSS_ACCESS_KEY_ID     "klado$(random | tr -dc 'A-Za-z0-9' | cut -c1-20)"
  set_env OSS_ACCESS_KEY_SECRET "$(random)"
  set_env AUTH_ADMIN_EMAILS    "$ADMIN_EMAIL"
  # 新装默认不开认证，但绑回环，两者配套：只有本机能访问。
  set_env AUTH_ENABLED         "false"
  chmod 600 .env
  say "已写入随机密钥、管理员邮箱 $ADMIN_EMAIL"
fi

# ⚠️ 判据在这里，不在「我调用了写入函数」上面。两个分支都要过：已存在的
# .env 可能是坏文件，而「生成了一份新的」这一支最需要检查，因为刚才那两个
# 正则/参数 bug 正是让它一个值都没写进去、却照样报告成功。
verify_secrets

# 已经在 .env 里的值优先于命令行给的值 —— 只在没有设置时才写绑定地址。
if ! grep -qE '^KLADO_APP_BIND=' .env; then
  printf 'KLADO_APP_BIND=%s\nKLADO_ADMIN_BIND=%s\n' "$BIND" "$BIND" >> .env
fi

# ── 4. 起服务 ──────────────────────────────────────────────────────────────
say "构建镜像（第一次会久一些，要装依赖）"
docker compose build app
say "启动"
docker compose up -d

# ── 5. 等它真的能用了 ──────────────────────────────────────────────────────
# 容器起来 ≠ 能用。健康接口过、且首页可达才算完；后者才是「401 陷阱」会露馅
# 的地方——不检查的话，脚本会打印一个打不开的地址然后报告成功。
URL="http://${BIND/#0.0.0.0/127.0.0.1}:$APP_PORT"
ok=0
for i in $(seq 1 60); do
  if curl -fsS "$URL/api/health" >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
[[ $ok -eq 1 ]] || {
  warn "120 秒内没等到 /api/health。看看日志： docker compose logs --tail=50 app"
  exit 1
}

# 首页必须返回 200 而不是 401 —— 这正是上面第 2 条那个坑的判据。
home_status="$(curl -o /dev/null -s -w '%{http_code}' "$URL/" || true)"
if [[ "$home_status" != "200" ]]; then
  die "首页返回 $home_status 而不是 200。多半是 .env 里的 AUTH_ADMIN_EMAILS 是空的：
      没有它，单用户模式下每个 /api 请求都会 401。填上邮箱再跑一次本脚本。"
fi

cat <<EOF

  ✓ Klado 起来了

    应用      $URL
    管理后台  http://${BIND/#0.0.0.0/127.0.0.1}:$ADMIN_PORT
    登录邮箱  $ADMIN_EMAIL

  接下来：
    · 停止    docker compose stop
    · 看日志  docker compose logs -f app
    · 想让局域网里别人访问，先把 .env 里的 AUTH_ENABLED 改成 true，
      再把 KLADO_APP_BIND 改成 0.0.0.0，然后 docker compose up -d

EOF