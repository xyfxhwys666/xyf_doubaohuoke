# ERP 直推轻量化设计

## 目标

采集任务发现有效客户线索后，立即按照 ERP 客户线索接收接口的字段规范推送，不要求部署 PostgreSQL 或 Redis。页面继续实时展示采集结果，并保留手动下载能力。

## 存储边界

- 业务线索、任务进度和操作日志沿用 `data/` 下的 UTF-8 JSON 文件持久化，支持任务中断后查看、继续处理和下载。
- ERP 推送失败记录写入本地 JSON 失败队列，保存线索标识、失败原因、尝试次数和最后尝试时间，可由接口手动重试。
- 不把业务线索写入 PostgreSQL，不依赖 Redis、Celery 或 Docker 服务。
- 账号与权限模块继续使用 SQLAlchemy，但本地默认连接 SQLite 文件，因此无需安装独立数据库服务。

## 数据流

1. 用户用自然语言创建任务并选择豆包、DeepSeek 或多模型。
2. 模型按城市发现公司，豆包逐家公司联网补充联系方式。
3. 每批合格线索完成去重后先写入本地任务 JSON，确保页面立即可见。
4. 如果配置了 ERP 接收地址，系统逐条映射为 ERP JSON 并立即推送。
5. 只有 HTTP 成功且 ERP 响应 `errCode == 0` 才标记成功；其余结果进入失败队列，不影响后续公司继续采集。
6. 用户可以重试失败项，也可以按城市下载 CSV、XLSX 或 JSON。

## ERP 映射

- `customerAccountGuid`：使用城市和标准化公司名称生成稳定 UUID，保证重复推送执行更新而不是新增重复客户。
- `customerAccountName`：公司名称。
- `customerProfileBio`：清洗后的服务介绍，不混入电话号码等联系方式。
- `customerIpRegion`：城市。
- `customerType`：`企业客户`。
- `cEndCustomerRequirement`：用户原始自然语言需求。
- `sourceMethod`：`AI联网采集`。
- 手机、邮箱、微信写入 `contactInfo` 对应字段；地址和来源链接写入 `contactInfo.otherContact`。

## 配置与安全

ERP 地址和鉴权信息只从 `.env` 读取，前端不展示密钥。未配置 ERP 时采集和下载仍正常工作，界面显示“未配置自动推送”。日志不得写入 API 密钥或完整鉴权头。

## 验收标准

- 未安装 PostgreSQL、Redis 或 Docker 时，后端可以启动并完成采集任务。
- 有效线索能在本地展示并按 ERP 文档结构逐条推送。
- ERP `errCode != 0`、网络异常和超时均被记录，单条失败不会中断整个任务。
- 同一城市同一公司重复推送使用相同 `customerAccountGuid`。
- 失败记录可重试，成功后从待重试队列移除并保留操作日志。
- 下载功能和多模型采集流程不受影响。
