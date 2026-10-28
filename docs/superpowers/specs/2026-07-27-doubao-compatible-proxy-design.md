# 豆包 OpenAI 兼容代理接入设计

## 目标

将豆包提供方从火山方舟接入点切换为 `api.kuai.host` 的 OpenAI 兼容接口，使用 `doubao-seed-2-0-lite-260428`，API Key 由用户最后写入 `.env`。

## 配置

```env
DOUBAO_API_KEY=
DOUBAO_BASE_URL=https://api.kuai.host/v1
DOUBAO_MODEL=doubao-seed-2-0-lite-260428
```

## 兼容规则

- 火山方舟地址仍要求 `DOUBAO_MODEL` 为 `ep-` 开头的推理接入点。
- 非火山方舟的 OpenAI 兼容地址允许直接填写模型名称。
- API Key 为空时，前端必须继续显示豆包“未配置”。
- 不读取、不记录、不提交用户填写的 API Key。

## 验证

- 自动测试覆盖方舟接入点限制和第三方直连模型名称。
- 用户填入 API Key 后，重新启动后端并发送最小 JSON 请求验证配置。

