# Storage Backends

LuomoFile Hub 在 SQLite 中保存文件元数据，在一个启用的后端中保存对象内容。每条记录都应能解析到唯一对象键；切换后端不会自动迁移旧对象。

## Local temp

本地后端适合开发和有 TTL 的小文件。`LOCAL_TEMP_PATH` 必须位于持久卷内，否则重建容器会丢失内容。`LOCAL_TEMP_MAX_CAPACITY_BYTES` 是调度提示，不是文件系统配额。

## R2 / S3-compatible

启用 `R2_ENABLED` 前设置 account、access key、secret、bucket 和公开基础 URL。使用只允许目标 bucket 所需操作的专用凭据，不要使用账户级全权 token。

## Mounted COS

挂载式后端由宿主机负责认证与挂载。Compose 的 `COS_HOST_MOUNT_PATH` 和 `COS_SV_HOST_MOUNT_PATH` 控制宿主路径，应用内路径保持 `/mnt/cos` 与 `/mnt/cos_sv`。

启动前验证：

```bash
test -d "$COS_HOST_MOUNT_PATH"
test -w "$COS_HOST_MOUNT_PATH"
docker compose run --rm luomofile-hub python scripts/refresh_storage_usage.py
```

不要在挂载失效时把空目录当作健康存储。生产环境应同时检查挂载类型、可写性和已用空间。

## Consistency and recovery

- 删除流程应先更新状态，再删除对象并记录审计结果。
- 数据库恢复后，抽样验证对象键仍存在。
- 对象存储恢复后，运行使用量刷新并检查孤儿对象。
- 公共链接密钥一旦轮换，旧签名链接将失效；轮换前应通知使用者。
