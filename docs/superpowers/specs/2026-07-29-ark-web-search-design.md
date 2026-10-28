# 方舟联网搜索接入设计

## 目标

让官方火山方舟豆包在候选公司发现和单家公司联系方式补全时使用 Responses API 的 `web_search` 工具，减少依赖模型记忆产生的错误公司与联系方式。

## 范围

- 官方方舟地址下的豆包请求改用 `/responses`。
- 候选公司发现、普通线索发现和单家公司补全启用 `web_search`。
- 关键词解析和基于已有证据的相关性判断不启用联网搜索，避免无意义费用。
- DeepSeek 和兼容中转站继续使用 `/chat/completions`。
- 不修改前端模型选择、任务并发、真实性按钮或 ERP 字段映射。
- 不放宽 ERP 推送条件；模型或联网搜索返回的数据仍需经过现有独立证据门槛。

## 结构

在现有 `OpenAICompatibleVerifier` 中增加明确的方舟 Responses 模式。公共业务方法继续生成原有提示词，底层请求方法根据服务地址选择协议：官方方舟使用 Responses API，其他提供方使用 Chat Completions。

Responses 请求使用当前 Endpoint ID 作为 `model`，将 system 和 user 内容放入 `input`，需要联网时附带 `web_search` 工具及有限的关键词、结果数和工具调用次数。响应解析只接受消息中的 `output_text`，去除 Markdown 代码块后解析 `items` JSON。

## 数据流

1. 用户选择豆包并提交自然语言任务。
2. 关键词解析使用方舟 Responses API，但不调用联网工具。
3. 候选发现使用联网搜索获取真实公司候选。
4. 单家公司补全再次联网，获取手机号、来源 URL 与证据原文。
5. 结果先进入现有采集结果与证据核验流程。
6. 只有满足现有 ERP 严格门槛的数据才允许自动推送。

## 错误处理

- HTTP 错误、余额不足、响应结构缺失或 JSON 非法统一转换为现有 `ModelVerificationError`。
- 错误信息不包含 API Key、Authorization 头或完整第三方响应。
- 联网搜索失败不伪造回退数据，由现有任务层记录异常并继续后续任务。

## 测试

- 验证官方方舟调用 `/responses`，且公司发现与补全请求包含 `web_search`。
- 验证关键词解析与证据判断不包含 `web_search`。
- 验证 Responses `output_text` 可以转换为现有业务结构。
- 验证 DeepSeek 和中转站仍调用 `/chat/completions`。
- 运行模型层测试、采集服务回归测试和前端构建。

## 验收标准

- 官方豆包不再调用 `/chat/completions`。
- 联网搜索仅用于需要外部事实的采集阶段。
- 没有额度时返回可诊断错误，服务本身保持可用。
- 原有 DeepSeek、中转站、展示和 ERP 严格推送行为不回归。
