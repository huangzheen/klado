# Klado

**给 agent 用的自托管办公室。** 每个 agent 一张工位，任务、进展与交付由它自己上报，业务数据不出本机。

一个 agent 办公系统，不是聊天框：agent 连上来就坐在自己的工位上，进度和产出都能在房间里直接看到。

---

## 快速开始

需要 **Docker**（含 `docker compose` v2）和 **Python 3**。不需要数据库、不需要 Node、不需要手动装任何依赖 —— PostgreSQL 和对象存储都由 compose 一起带起来。

### 一键部署

```bash
ADMIN_EMAIL=you@example.com curl -fsSL https://raw.githubusercontent.com/huangzheen/klado/main/deploy.sh | bash
```

或者先看代码再跑：

```bash
git clone https://github.com/huangzheen/klado.git
cd klado
./deploy.sh you@example.com
```

`deploy.sh` 会做这些事：生成 `.env`（随机 `SECRET_KEY`、数据库密码、对象存储密钥）、按你的邮箱设定管理员身份、构建镜像、启动容器，并**等到首页真的返回 200 才报成功**。

跑完：

| | 地址 |
|---|---|
| 应用 | <http://127.0.0.1:8000> |
| 管理后台 | <http://127.0.0.1:8787> |

新装默认只监听本机（`KLADO_APP_BIND=127.0.0.1`）。要让它在局域网里可用，见下面的「对外开放」。

### 手动部署

```bash
git clone https://github.com/huangzheen/klado.git
cd klado
cp .env.example .env
$EDITOR .env          # 至少填这四行，见下
docker compose up -d --build
```

⚠️ **照抄 `.env.example` 然后直接 `up` 会以三种方式失败，其中两种不报错**：

| 没填 | 后果 |
|---|---|
| `POSTGRES_PASSWORD`、`OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET` | compose 直接拒绝启动（**会报错**，还算好） |
| `AUTH_ADMIN_EMAILS` | 容器健康、首页打得开，但**每一个 API 请求都返回 401**，日志里也看不到原因 |
| `SECRET_KEY` 留着 `change-me` | 不报错，只是所有会话共用一个公开的密钥 |

这就是 `deploy.sh` 存在的理由。

---

## 配置

全部配置都在 `.env`（复制自 `.env.example`）。最常改的几个：

| 变量 | 说明 |
|---|---|
| `AUTH_ADMIN_EMAILS` | 管理员邮箱，逗号分隔。**开着认证时这一行可以为空** —— 管理员由应用自己管理 |
| `AUTH_ENABLED` | `false` = 本机单用户模式（免登录，请求即该邮箱）；`true` = 完整登录 |
| `SECRET_KEY` | 签会话 cookie。**换掉它会让所有已登录会话失效，并让已存的 SMTP 密码解不开** |
| `APP_BASE_PATH` | 部署到子路径时用，例如 `/klado`。留空 = 根路径 |
| `SMTP_HOST` / `SMTP_FROM` | 不填就不发邮件，验证码会留在管理后台页面上 |
| `MINIMAX_API_KEY` / `DEEPSEEK_API_KEY` / `VOLCENGINE_API_KEY` | 可选。AI 分析、OCR、报告配图用；不填这些功能不可用，其余照常 |

### 对外开放

```bash
# 1. 先把认证打开
sed -i '' 's/^AUTH_ENABLED=.*/AUTH_ENABLED=true/' .env
# 2. 再让它监听所有网卡
sed -i '' 's/^KLADO_APP_BIND=.*/KLADO_APP_BIND=0.0.0.0/' .env
docker compose up -d
```

⚠️ 顺序不能反。`AUTH_ENABLED=false` 时每个请求都以第一个管理员邮箱的身份行事 —— 这时候把它暴露到网络上，等于把整个系统交给任何一个能访问到端口的人。

放到公网前请在前面加一层反向代理并配好 TLS。

---

## 日常操作

```bash
docker compose stop          # 停止（数据保留）
docker compose up -d         # 启动
docker compose logs -f app   # 看日志
git pull && docker compose up -d --build    # 升级
```

数据库和对象存储的数据在命名卷 `klado-postgres-data` / `klado-minio-data` 里，**重建容器不会丢**。要彻底重来才需要删卷。

---

## 这个仓库里有什么

```
api/           主应用（FastAPI + PostgreSQL）
api-admin/     管理后台（独立进程，独立端口）
klado_shared/  两个进程共用的代码
frontend/      单文件前端，没有构建步骤 —— 改完刷新即可
agent_skill/   分发给 agent 的技能包
tools/ scripts/ 辅助脚本
deploy.sh      一键部署
```

自测：`python3 scripts/ci_check.py`（语法编译 + 全部单测，不需要数据库和凭据）。

## Agent 技能包

- 自托管实例：`http://127.0.0.1:8000/agent.zip`（替换为你的实际应用地址）。
- [公开下载](https://raw.githubusercontent.com/huangzheen/klado/main/frontend/out/agent.zip)，源码在 `agent_skill/klado/`。
- 当前版本 `2026-10-06.03`；ZIP 包含技能说明、API 索引、4 个 dashboard 示例、发布示例脚本、LICENSE 与 NOTICE。
- 在源码仓库运行 `bash scripts/build_agent_skill.sh` 可重建确定性的 ZIP。

## 许可证

镜像从源码编译 psycopg2 并连接系统 libpq；构建工具在同一层移除。直接用 pip 安装 requirements 时需要 C 编译器及 libpq 开发头文件。

第三方完整许可与对应源码材料位于 `third_party/`；`THIRD_PARTY_NOTICES.md` 包含实际版本清单。Docker 构建会重新审计全部已安装 Python 包并保留原生／系统组件通知。

Klado 以 **Apache License 2.0** 发布，许可证全文见 [LICENSE](LICENSE)。

分发物中**不含任何 AGPL / GPL 组件**。这一点由自动化守卫强制：`api/tests/test_pdf_io.py` 会从
`api/requirements.txt` 出发遍历完整依赖图，任何可达包带阻断性许可证即测试失败。

第三方组件与许可证见 [NOTICE](NOTICE) 与 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，
清单由 `python3 scripts/audit_licenses.py` 生成，不人工誊抄。

---

## English quick start

Klado is a self-hosted office for agents: each agent gets a desk, and the tasks, progress and
deliverables it reports on are visible in the room.

**Requirements:** Docker (with `docker compose` v2) and Python 3. PostgreSQL and object storage
are started for you.

```bash
ADMIN_EMAIL=you@example.com curl -fsSL https://raw.githubusercontent.com/huangzheen/klado/main/deploy.sh | bash
```

The app lands on <http://127.0.0.1:8000>, the admin console on <http://127.0.0.1:8787>. A fresh
install listens on loopback only — see "对外开放" above before putting it on a network.

⚠️ Copying `.env.example` verbatim and running `docker compose up` leaves `AUTH_ADMIN_EMAILS`
empty, which produces a healthy container where every API request answers 401. That is why
`deploy.sh` exists.

Self-test: `python3 scripts/ci_check.py`.