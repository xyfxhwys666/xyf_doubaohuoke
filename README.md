# 多城市 B 端客户线索采集与核验平台

这是一个面向 B 端销售团队的客户线索采集、核验、清洗、去重和交付平台。

用户可以输入城市、行业和联系方式要求，系统通过公开网页、地图数据、模型能力和可选的豆包网页通道发现候选公司，再对公司主体、联系方式和来源证据进行核验，最后导出文件或推送到 ERP。

## 主要能力

- 多城市、多个关键词的长任务采集
- 公司主体、手机号、邮箱和来源证据核验
- 线索规范化、任务内去重和跨任务交付去重
- 任务进度、暂停、恢复、终止和检查点
- CSV、JSON、Excel 和 ZIP 导出
- ERP 推送、业务结果判断和幂等账本
- Playwright + Chrome CDP + 固定 Profile 的网页自动化

## 技术栈

- 前端：React、TypeScript、Vite
- 后端：Python 3.12、FastAPI、Uvicorn
- 关系数据：SQLAlchemy、SQLite、Alembic
- 任务状态：JSON TaskStore
- 外部服务：HTTPX、公开网页、高德、DeepSeek、豆包/Ark
- 自动化与质量：Playwright、pytest、Ruff

## 目录结构

```text
backend/     FastAPI 后端、采集服务、数据模型和测试
frontend/    React + TypeScript 前端
docs/        项目规格、运行说明和开发文档
.env.example 配置示例，不包含真实密钥
```

## 本地运行

项目建议使用 Python 3.12 和 Node.js 22。也可以按照 `environment.yml` 创建 Conda 环境。

### 1. 配置后端

```powershell
Copy-Item .env.example .env
Set-Location backend
python -m pip install -e ".[dev]"
Set-Location ..
```

根据实际使用的数据源，在 `.env` 中填写对应配置。不要提交 `.env`、API Key、Cookie、Token 或浏览器 Profile。

### 2. 启动后端

```powershell
python -m uvicorn app.main:create_app --factory --app-dir backend --host 127.0.0.1 --port 8010
```

### 3. 启动前端

另开一个终端：

```powershell
Set-Location frontend
npm install
npm run dev
```

启动后访问 `http://127.0.0.1:5173`。前端开发服务器会将 `/api` 请求代理到 `http://127.0.0.1:8010`。

## 测试与检查

```powershell
python -m pytest backend/tests -q
python -m ruff check backend/app backend/tests
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend run build
```

完整的零基础说明、调用链、数据模型和排查方法见：

[`docs/项目零基础上手与实习工作详解.md`](docs/项目零基础上手与实习工作详解.md)

## 数据与安全边界

当前 MVP 使用 SQLite 保存身份和权限等关系数据，使用 JSON 文件保存采集任务运行状态。运行数据、导出文件和浏览器 Profile 默认位于 `data/`，不会提交到 Git。

模型和网页只能发现候选信息，不能直接代表事实。只有经过主体、联系方式和来源证据检查的线索才应进入正式交付。

