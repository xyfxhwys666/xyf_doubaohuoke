# PostgreSQL 采集存储设计

## 目标

将采集任务、城市进度、客户线索和执行日志从 JSON 文件迁移到 PostgreSQL，使单个全国任务可以持续运行数天并保存数万条公司数据。本阶段不引入 Redis 或 Celery。

## 数据表

### `collection_tasks`

保存任务配置和汇总进度：自然语言需求、城市、关键词、候选模型、联系方式规则、任务状态、采集阶段、候选数、补采进度、错误数、创建账号、心跳时间和运行租约。城市、关键词、模型等体积较小的配置使用 JSON 字段；不会在该表保存全部线索。

### `collection_leads`

每家公司一行，包含城市、规范化公司名称、手机号、邮箱、微信、地址、服务简介、保存时间、候选来源、来源链接、可信度、豆包补采状态和真实性验证状态。

唯一约束为 `task_id + city + normalized_company_name`，保证同一任务内的公司不会重复。手机号、邮箱、微信分别建立索引，写入时继续执行现有联系方式去重逻辑。

### `collection_city_progress`

每个任务、每个城市一行，保存城市状态、候选目标、候选数量、补采数量和错误数。唯一约束为 `task_id + city`。

### `collection_activity`

每次创建、启动、候选发现、豆包补采、重试、验证和 API 推送保存一行日志，包含操作账号、时间、城市、关键词、渠道、结果和错误详情。

## 持久化接口

以 `PostgresTaskStore` 替换 JSON `TaskStore`。任务状态、线索、城市进度和日志分别写入，避免每完成一家公司就重写整个任务。

- `create_task(task)`：在一个事务中创建任务和城市进度。
- `load_task(task_id, include_leads)`：按需加载任务；进度查询不读取数万条线索。
- `save_task_state(task)`：只更新任务汇总字段。
- `upsert_leads(task_id, leads)`：批量新增或更新本次发生变化的线索。
- `save_city_progress(task_id, progress)`：更新一个城市的进度。
- `append_activity(task_id, entries)`：追加新日志，不重写旧日志。
- `list_leads(task_id, city, offset, limit)`：数据库分页读取结果。

服务层保留现有采集流程，但每次发现候选或完成豆包查询后立即提交对应线索和任务进度。

## 长时间任务与断点恢复

任务启动时写入运行租约和心跳。单个 FastAPI 实例在后台线程执行任务并定期刷新心跳。服务启动时扫描状态为 `running` 且租约已过期的任务，将其恢复为 `queued` 并从数据库中尚未完成的城市或线索继续。

不使用 Redis 时仅支持单个后端实例执行采集任务。若以后需要多个实例并行，再增加 Redis/Celery 或完善 PostgreSQL `SKIP LOCKED` 工作者队列。

## 历史 JSON 导入

首次启动 PostgreSQL 存储时扫描 `data/tasks/*.json`：

1. 按原任务 UUID 幂等创建任务。
2. 批量导入城市进度、线索和活动日志。
3. 已存在的任务跳过，保证重复启动不会重复导入。
4. 原 JSON 文件保持不变，作为迁移备份。

## API 兼容

现有创建、启动、状态、线索、验证、下载和推送 API 路径保持不变。线索接口增加可选 `offset` 和 `limit`，前端当前任务仍可使用默认第一页；导出和 API 推送通过数据库分批遍历全部线索。

## Redis 清理

- 从 `compose.yaml` 删除 Redis 服务。
- 从后端依赖删除 `redis` 和 `celery`。
- 从 `Settings` 和环境示例删除必填 `REDIS_URL`。
- 不修改现有豆包、DeepSeek 和 PostgreSQL 密钥配置。

## ERP 标准推送与 Outbox

每条线索写入 PostgreSQL 后，按照《ERP 客户线索接收接口文档》的英文 JSON 字段生成推送任务。数据库新增 `lead_delivery_outbox` 表，保存目标地址、标准请求报文、推送状态、尝试次数、下次重试时间、成功时间、ERP 错误信息和响应摘要。

字段映射固定为：

- `sourceAccountName`：执行采集的账号或模型账号名称。
- `customerAccountGuid`：根据城市和规范化公司名称生成的稳定 UUID，同一公司重复推送时由 ERP 更新原线索。
- `customerSourceChannel`：候选发现渠道或豆包。
- `customerCapturedAt`：线索保存时间。
- `customerAccountName`：公司名称。
- `customerProfileBio`：服务简介。
- `customerIpRegion`：城市。
- `customerType`：`企业客户`。
- `cEndCustomerRequirement`：用户自然语言任务。
- `sourceMethod`：`AI联网采集`。
- `contactInfo.mobilePhone`、`contactInfo.email`、`contactInfo.wechatId`：对应联系方式字符串。
- `contactInfo.otherContact`：地址和公开来源链接。

ERP 推送成功必须同时满足 HTTP 请求成功且响应 JSON 的 `errCode` 等于 `0`。HTTP 200 但 `errCode != 0` 仍记为失败。失败记录按退避策略自动重试；相同线索和目标地址只保留一个 outbox 记录。

## 数据可信度

豆包联网询问结果作为主要采集结果保存，但 `discovery_channel`、`source_url`、`source_confidence` 和 `verification_status` 必须保留。未执行真实性验证的数据继续显示“待验证”，不得因为写入 PostgreSQL 自动变成“已验证”。

## 测试与验收

- 使用 SQLite 测试数据库验证仓储接口和 API 兼容性。
- 使用 PostgreSQL 16 执行 Alembic 迁移和真实集成测试。
- 验证同一任务内公司去重、联系方式索引、分页、逐条提交和 JSON 幂等导入。
- 模拟进程中断，验证重启后从未完成线索继续。
- 导入现有广州任务并确认记录数量、联系方式字段和豆包补采模型保持一致。
- 验证 ERP 字段映射、稳定 `customerAccountGuid`、`errCode` 判断、失败重试和幂等 outbox。
