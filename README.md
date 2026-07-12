# LuomoFile Hub

LuomoFile Hub 是一个“默认私有、按需分享”的自托管文件中心。它把普通文件、临时文件和图片放进同一套元数据与权限模型，再把实际内容分配到本地目录、S3 兼容对象存储或挂载式 COS 后端。

它不是公开网盘脚本。上传后的文件默认只对所有者可见；公开访问必须由所有者显式生成签名链接，并且可以随时撤销。

## Product rules

- **Private by default**：登录用户只能看到自己的文件，管理员另有审计视图。
- **Links are capabilities**：公开链接带签名状态，生成与撤销都记录审计事件。
- **Purpose-aware limits**：普通文件、图片、临时文件和匿名上传分别限额。
- **Storage is pluggable**：R2/S3 API、COS 挂载和本地临时目录可独立启停。
- **Temporary means temporary**：后台任务定时清理过期记录与内容。
- **API and UI share policy**：网页上传、开发者 API 与内部接口复用同一文件服务逻辑。

## Quick start with local storage

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

至少为 `SESSION_SECRET`、`PUBLIC_LINK_SECRET` 和 `LUOMOFILE_INTERNAL_TOKEN` 生成三个不同的随机值。然后设置管理员密码并启动：

```bash
mkdir -p data/cos data/cos-sv
docker compose build
docker compose run --rm luomofile-hub python scripts/set_admin_password.py
docker compose up -d
curl http://127.0.0.1:8791/health
```

默认示例关闭 R2 和 COS，只启用 `/app/data/tmp`。服务映射到 `127.0.0.1:8791`，适合放在 HTTPS 反向代理或 Tunnel 后面。

## Storage map

| Backend | Connection | Typical role |
| --- | --- | --- |
| Local temp | Container volume | 开发、临时内容、降级路径 |
| R2 / S3-compatible | `boto3` API credentials | 对象存储与独立公开域名 |
| COS primary | Host mount at `/mnt/cos` | 大文件与已有挂载目录 |
| COS secondary | Host mount at `/mnt/cos_sv` | 异地副本或第二节点 |

配置和故障排查见 [docs/STORAGE.md](docs/STORAGE.md)。

## Interfaces

网页端提供注册、邮箱验证、上传、文件列表、标签、图片库、临时文件和管理员存储视图。开发者接口集中在 `/api/v1`；跨服务调用使用 `/api/internal` 并要求独立 Bearer token。

常用探针：

```text
GET /health
GET /api/public/status
GET /api/public/version
GET /api/public/storage
```

## Deployment checklist

1. 将 `.env` 权限设为 `600`，不要复用三个签名/会话 token。
2. 保持 `ALLOW_ANONYMOUS_UPLOAD=false`，除非已配置 WAF、验证码与限流。
3. 把 8791 保持在回环地址，只通过 HTTPS 暴露。
4. 为数据库与对象内容分别设计备份；只备份 SQLite 不等于备份文件。
5. 定期运行 `scripts/refresh_storage_usage.py`，并观察清理任务日志。

安全边界与报告方式见 [SECURITY.md](SECURITY.md)。

## License

[MIT](LICENSE) © 2026 Luomo
