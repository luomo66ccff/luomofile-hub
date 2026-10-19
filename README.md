# LuomoFile Hub

LuomoFile Hub 是洛墨站的自托管文件中心，线上运行于 <https://file.luomo.moe>。核心规则是**默认私有，按需分享**：文件上传后只有所有者可见，公开访问必须由所有者生成签名链接，并且随时可以撤销。

当前版本 `0.5.0-harbor-t001` 按“文件星港”重新设计：文件像停靠港口的货舱，上传叫“靠岸”，相册是展厅，存储后端是不同的泊位。

## 功能一览

| 模块 | 内容 |
| --- | --- |
| 文件 | 拖拽上传、标签、批量删除与撤销链接、详情页；`Ctrl/⌘ + K` 或 `/` 打开命令面板，搜索文件和页面 |
| 分享 | 签名链接 `/f/{id}/{name}`，生成与撤销都记审计；临时分享 `/t/{id}` 到期自动清理 |
| 图片 | 图片库、灯箱浏览；缩略图 480px、预览图 1600px，派生图只保留像素，去掉 EXIF / GPS / XMP / ICC |
| 相册 | 展示范围分三档：`restricted`（仅受邀成员）、`authenticated`（登录用户）、`public`；支持邀请、成员投稿、审核与上传者同意 |
| 订阅 | 管理员为订阅文件签发 `lsub_` 令牌，`/sub/{token}` 按客户端输出 5 种格式；每条链接可绑定独立的节点身份 |
| 开发者 | `/api/v1` 文件、图片、临时文件上传与链接管理，使用 API key |
| 管理后台 | 全站文件审计、存储泊位、API key、订阅管理、手动清理 |

### 订阅格式

`/sub/{token}?format=...` 支持：

| `format` | 适用客户端 |
| --- | --- |
| `mihomo`（默认） | Mihomo / Clash Verge，保留完整节点 |
| `clash` | Clash for Windows 等旧内核 |
| `base64` | v2rayN / v2rayNG / Shadowrocket |
| `uri` | 通用分享链接，每行一个节点 |
| `sing-box` | sing-box 1.12+ 桌面代理 |

令牌在数据库中加密保存（`SUBSCRIPTION_TOKEN_ENCRYPTION_SECRET`，未设置时回落到 `SESSION_SECRET`）。开启 `SUBSCRIPTION_IDENTITY_SYNC_ENABLED` 后，每条链接会派生独立的节点凭据，并通过 SSH 同步到 `SUBSCRIPTION_IDENTITY_TARGETS` 列出的节点；吊销链接后，对应凭据会从节点上同步移除。

## 快速开始（本地存储）

需要 Docker 与 Compose。示例 Compose 会接入外部网络 `luomocore_default`，第一次运行前先创建：

```bash
docker network create luomocore_default
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

至少为 `SESSION_SECRET`、`PUBLIC_LINK_SECRET`、`LUOMOFILE_INTERNAL_TOKEN` 各生成一个**不同的**随机值。然后设置管理员密码并启动：

```bash
mkdir -p data/cos data/cos-sv
docker compose build
docker compose run --rm luomofile-hub python scripts/set_admin_password.py
docker compose up -d
curl http://127.0.0.1:8791/ready
```

示例配置只启用本地临时目录 `/app/data/tmp`，R2 与 COS 默认关闭。服务映射到 `127.0.0.1:8791`，适合放在 HTTPS 反向代理或 Tunnel 之后。

`Dockerfile` 用于全新构建；`Dockerfile.release` 是生产环境在已有镜像上做增量发布用的，不适合直接拿来构建。

## 存储泊位

| 后端 | 接入方式 | 典型用途 |
| --- | --- | --- |
| 本地临时目录 | 容器卷 | 开发、临时文件、降级路径 |
| R2 / S3 兼容 | `boto3` API 凭据 | 对象存储与独立公开域名 |
| COS 主挂载 | 宿主机挂载到 `/mnt/cos` | 大文件与已有挂载目录 |
| COS 副挂载 | 宿主机挂载到 `/mnt/cos_sv` | 异地副本或第二节点 |
| 私有媒体 | 持久卷，不挂公开 URL | 相册与图片原图、派生图 |

元数据保存在 SQLite，对象内容保存在其中一个启用的后端；切换后端不会迁移旧对象。图片处理会保留 `MEDIA_DISK_RESERVE_BYTES` 的磁盘余量，空间不足时拒绝写入而不是把盘写满。配置与故障排查见 [docs/STORAGE.md](docs/STORAGE.md)。

## 接口

| 前缀 | 认证 | 用途 |
| --- | --- | --- |
| `/api/v1` | API key | 开发者上传、列表、详情、生成与撤销链接、存储统计 |
| `/api/albums` | 登录会话 | 相册、条目、邀请与成员管理 |
| `/api/internal` | `LUOMOFILE_INTERNAL_TOKEN`（Bearer） | 跨服务上传、查询与指标 |
| `/api/public` | 无 | 状态、版本、存储概况 |

接口文档登录后在 `/developer/docs` 查看。健康检查：

```text
GET /health               进程存活
GET /ready                数据库与 SESSION_SECRET（至少 32 字符）检查；未就绪返回 503
GET /api/public/status
GET /api/public/version
GET /api/public/storage
```

## 配置

完整列表见 [.env.example](.env.example)。常用项：

| 变量 | 作用 |
| --- | --- |
| `SESSION_SECRET` / `PUBLIC_LINK_SECRET` / `LUOMOFILE_INTERNAL_TOKEN` | 会话、签名链接、内部接口令牌，三者不能复用 |
| `APP_BASE_URL` | 对外地址，用于链接与邮件 |
| `MAX_UPLOAD_BYTES` / `MAX_IMAGE_BYTES` / `MAX_TEMP_BYTES` | 普通文件、图片、临时文件的上传上限 |
| `ALLOW_ANONYMOUS_UPLOAD` | 匿名临时上传；代码默认开启，示例配置为 `false` |
| `IMAGE_MAX_PIXELS` / `IMAGE_MAX_DIMENSION` / `IMAGE_MAX_FRAMES` | 图片解码上限，防解压炸弹 |
| `R2_*` / `COS_*` / `COS_SV_*` / `LOCAL_TEMP_*` | 各存储泊位开关、凭据与容量 |
| `SUBSCRIPTION_IDENTITY_*` | 订阅身份派生与 SSH 同步 |
| `SMTP_*` | 注册邮箱验证 |

## 部署清单

1. `.env` 权限设为 `600`，三个签名/会话令牌各不相同。
2. 除非已有 WAF、验证码与限流，保持 `ALLOW_ANONYMOUS_UPLOAD=false`。
3. 8791 只监听回环地址，对外只走 HTTPS。
4. 数据库、对象内容、私有媒体分别备份；只备份 SQLite 不等于备份了文件。
5. 定期运行 `scripts/refresh_storage_usage.py`，留意清理任务日志。
6. 启用订阅身份同步时，SSH 私钥与 `known_hosts` 通过 secrets 挂载（默认 `/run/secrets/`），不要放进镜像。

安全边界与报告方式见 [SECURITY.md](SECURITY.md)。

## 目录结构

```text
app/
  main.py                应用装配、中间件、后台任务
  routes/                按领域拆分的路由：auth、files、links、temp_share、gallery、admin、api_v1、internal_api、public…
  storage/               存储后端（local_temp、r2、cos、router）与私有媒体、磁盘余量
  album_service.py       相册权限与分享
  image_media.py         图片校验与派生图
  subscriptions.py       订阅令牌与链接；subscription_formats.py 负责格式转换
  identity_sync.py       节点身份 SSH 同步
  web/templates/         layouts / pages / partials / macros
  web/static/            harbor.css 设计系统、页面脚本、字体
scripts/                 运维脚本与测试脚本
docs/STORAGE.md          存储后端说明
```

## 运维脚本

| 脚本 | 用途 |
| --- | --- |
| `scripts/init_db.py` | 初始化数据库结构 |
| `scripts/set_admin_password.py` | 交互式设置管理员密码 |
| `scripts/create_api_key.py` | 创建开发者 API key |
| `scripts/refresh_storage_usage.py` | 刷新各后端用量 |
| `scripts/cleanup_once.py` | 立即执行一次过期清理 |
| `scripts/manage_subscription_links.py` | 命令行创建、列出、吊销订阅链接 |
| `scripts/reconcile_subscription_identities.py` | 重新核对并同步订阅节点身份 |

## 开发与测试

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -c requirements.lock
for t in scripts/test_*.py scripts/smoke_test_subscription_*.py; do python "$t" || break; done
```

以上测试自带临时数据库，不需要网络与真实凭据。另有 5 个 HTTP 冒烟测试（`smoke_test.py`、`smoke_test_public_pages.py`、`smoke_test_files_page.py`、`smoke_test_link_policy.py`、`smoke_test_upload_ui.py`）需要先用隔离的数据库启动一个服务，再通过 `SMOKE_BASE_URL` 与相同的 `DATABASE_PATH` 运行；具体环境变量见 CI。

CI（`.github/workflows/ci.yml`）在 Python 3.12 上执行 compileall、应用导入、全部自带测试，并启动隔离服务跑完这 5 个冒烟测试。

## License

[MIT](LICENSE) © 2026 Luomo
