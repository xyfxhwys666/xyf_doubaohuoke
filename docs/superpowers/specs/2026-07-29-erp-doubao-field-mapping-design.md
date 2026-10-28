# ERP 豆包字段映射设计

## 目标

按照《ERP 客户线索接收接口文档》生成客户线索 JSON，并统一 ERP 中豆包来源和 B 端客户类型的中文展示。

## 字段映射

| ERP 字段 | 固定值或来源 |
| --- | --- |
| `sourceAccountName` | `豆包` |
| `customerSourceChannel` | `豆包` |
| `customerType` | `B端客资` |
| `customerAccountGuid` | 按城市和规范化公司名称生成的稳定 UUID |
| `customerCapturedAt` | 线索保存时间，格式化为 `YYYY-MM-DD HH:mm:ss` |
| `customerAccountName` | 公司名称 |
| `customerProfileBio` | 服务简介 |
| `customerIpRegion` | 城市 |
| `cEndCustomerRequirement` | 用户自然语言任务 |
| `sourceMethod` | `AI联网采集` |
| `contactInfo` | 按文档嵌套手机、邮箱、微信和其他信息 |

## 数据流

采集结果进入 ERP 推送器后，由唯一的载荷构建函数完成字段转换。手动推送、自动推送和失败重试均复用同一载荷，不分别维护字段映射。

## 兼容与安全

- 保持 ERP 文档要求的英文 JSON 字段名和嵌套结构不变。
- 不在本次修改中调用真实 ERP 接口。
- 旧 ERP 记录不会自动更新；新推送或同一 `customerAccountGuid` 的后续更新才使用新值。
- 以单元测试断言三个中文值和完整文档字段结构，防止以后回退为 `doubao`、`AI客户采集工具` 或 `企业客户`。

## 验收标准

调用 `build_erp_payload` 后：

1. `sourceAccountName == "豆包"`。
2. `customerSourceChannel == "豆包"`。
3. `customerType == "B端客资"`。
4. ERP 文档列出的其余核心字段和 `contactInfo` 嵌套结构仍然存在。
5. 测试过程不产生真实网络推送。
