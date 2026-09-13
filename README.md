# echo

AI-powered Voice of Customer (VOC) engine built on MCP. Extract product insights & pain points from multi-platform comments.

**echo** 把散落在各个平台的用户评论，变成一份可执行的反馈报告：用户在抱怨什么、想要什么、喜欢什么、下一步该改什么。

适合 **开发者**（App Store 评论）、**博主/UP 主**（YouTube、B 站评论）、**商家/产品经理** 快速了解真实用户声音。

## 特性

- **多平台**：App Store、YouTube、Bilibili，粘贴链接自动识别平台
- **多模型**：DeepSeek、OpenAI、Claude、通义千问、Kimi、Gemini、OpenRouter、Ollama 本地模型，以及任意 OpenAI 兼容接口
- **结构化洞察**：情感分布、痛点（带严重度）、功能需求、好评点、高频疑问、优先级建议
- **证据可追溯**：每个主题的提及数和引用原文由程序根据真实评论计算，不由模型编造
- **可扩展到大量评论**：分批并发分析（map）+ 主题合并（reduce）
- **账号级增量同步**：跟踪 UP 主 / YouTube 频道 / App Store 开发者下的全部作品，只抓新增评论，存到本地 PostgreSQL
- **弹幕分析**（B 站）：弹幕密度时间轴、热点时刻识别与解读
- **RAG 问答**：直接问"最近 1 个月用户反馈较多的问题是什么"，排行类问题走 SQL 精确计数，开放问题走 bge-m3 向量检索，回答带原文引用
- **产品记忆**：按月份 / App 版本保存反馈快照，自动对比新增、上升、已解决的问题；结论写入 mem0 长期记忆
- **四种用法**：网页界面、命令行、Python 库、MCP Server（接入 Claude Code / Claude Desktop / Cursor / Cherry Studio 等）

## 快速开始

需要 Python 3.10+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/Palemoky/echo.git && cd echo
uv sync
cp .env.example .env   # 填入至少一个模型的 API Key
```

```bash
# 分析 App Store 应用评论（默认使用 DeepSeek）
uv run echo-voc analyze "https://apps.apple.com/cn/app/wechat/id414478124"

# 分析 B 站视频评论，用 Claude，输出中文报告到文件
uv run echo-voc analyze "https://www.bilibili.com/video/BV13dYu6aEPf" \
  --provider anthropic --lang Chinese -n 300 -o report.md

# 分析 YouTube 评论（需要 YOUTUBE_API_KEY），用本地 Ollama 模型
uv run echo-voc analyze "https://www.youtube.com/watch?v=_f7iYGxVSio" \
  --provider ollama --model qwen3

# 只抓取评论（不调用模型），之后再分析
uv run echo-voc fetch "https://www.bilibili.com/video/BV13dYu6aEPf" -o comments.json
uv run echo-voc analyze comments.json -f json -o report.json
```

其他命令：`echo-voc sources`（支持的平台）、`echo-voc providers`（模型预设）、`echo-voc --help`。

### 网页界面

不想记命令的话，所有功能都可以在本地网页中完成：

```bash
uv sync --all-extras
uv run echo-voc web          # 自动打开 http://127.0.0.1:8765
```

| 页面 | 功能 |
| --- | --- |
| 概览 | 数据统计、粘贴链接添加账号/作品、后台任务与同步记录 |
| 账号 / 作品 | 增量同步（弹幕、楼中楼、全量重扫）、计算向量、打标前估算 token、生成报告、按类型/情感/关键词浏览原文 |
| 分析 | 任意链接实时分析（不入库） |
| 弹幕 | 弹幕密度柱状图（高亮热点，点击跳转到视频对应时间）、热点解读、整体主题 |
| 问答 | RAG 提问，排行结果表格、引用编号可点击定位原文、参考 mem0 记忆 |
| 问题 | 按账号 / 时间窗 / 类型的问题排行，点击查看原文 |
| 趋势 | 生成月度或版本快照、勾选对比（情感折线图、问题提及柱状图、变化表）、搜索长期记忆 |
| 设置 | 数据库、Ollama/bge-m3、mem0、模型与凭据的连接状态（不显示密钥内容） |

耗时操作（同步、打标、报告等）在后台任务中执行，页面实时显示进度；同步和打标各自排队执行，避免并发触发平台风控或重复消耗 token。

安全说明：网页界面没有登录系统，默认只监听 `127.0.0.1`，并拒绝跨站请求和非本机 Host（防止恶意网页或 DNS 重绑定调用你的本地服务）。如需在局域网中使用，请设置 `ECHO_WEB_PASSWORD`（启用 HTTP Basic 认证）和 `ECHO_WEB_ALLOWED_HOSTS`，再以 `--host 0.0.0.0` 启动。

### 报告格式（示意）

```markdown
# VOC report: <应用 / 视频标题>

- **Platform:** appstore
- **Comments analyzed:** <N>
- **Average rating:** <x.x> / 5
- **Sentiment:** 👍 <x>% · 😐 <x>% · 👎 <x>%

## Summary

<3-5 句总结：整体口碑、最大问题、最受认可的点>

## Suggested actions

1. <按优先级排序的具体改进建议>

## 🔴 Pain points

### <痛点标题>

_<提及数> mention(s) (<占比>) · severity <1-5>/5_

<描述>

> <高赞 / 代表性原始评论> — 2★ · 👍 <点赞数>

## 💡 Feature requests

## 💚 What users love

## ❓ Recurring questions
```

## 本地工作区：增量同步、弹幕、RAG 与记忆

实时 `analyze` 不需要数据库；以下能力需要本地 PostgreSQL（pgvector）和 embedding 模型（默认 Ollama `bge-m3`）。

```bash
uv sync --all-extras          # 安装 store（数据库）与 memory（mem0）依赖
ollama pull bge-m3            # 1024 维中英多语言 embedding
docker compose up -d          # 启动 pgvector/pgvector:pg17，端口 5433
uv run echo-voc db init       # 建表 / 迁移
```

### 1. 跟踪账号并增量同步

```bash
# 跟踪整个账号（B 站需要 BILIBILI_COOKIE，YouTube 需要 YOUTUBE_API_KEY）
uv run echo-voc add https://space.bilibili.com/486906719
uv run echo-voc add https://www.youtube.com/@GoogleDevelopers
uv run echo-voc add https://apps.apple.com/cn/developer/wechat/id614694882
# 也可以只跟踪单个作品
uv run echo-voc add https://www.bilibili.com/video/BV13dYu6aEPf

uv run echo-voc sync --since 90d --danmaku      # 首次：抓近 90 天作品的评论和弹幕
uv run echo-voc sync --danmaku                  # 之后：只抓新增内容（可放进 cron 定时执行）
uv run echo-voc accounts && uv run echo-voc works
uv run echo-voc export -o feedback.jsonl --since 30d   # 导出做离线分析
```

增量策略：作品按发布时间倒序列出，遇到已入库作品即停止；评论按时间倒序抓取，读到早于上次水位（留 6 小时重叠以刷新点赞数）的连续评论后停止；弹幕默认每天最多刷新一次。每个作品独立提交，中断后重跑即可续传。所有请求按平台限速（B 站约 1 次/秒）并在 412/429 时指数退避。

### 2. 弹幕分析

```bash
uv run echo-voc report --work BV13dYu6aEPf --danmaku --lang Chinese -o danmaku.md
```

输出弹幕密度曲线、热点时间段（带跳转链接）及每个热点观众在吐槽/玩梗/提问什么，并对弹幕整体做主题分析（重复弹幕会合并计数）。

### 3. 打标、问题库与 RAG 问答

```bash
uv run echo-voc embed                  # 用 bge-m3 计算向量（增量）
uv run echo-voc label --since 90d      # LLM 标注情感并归入账号的问题库（增量；执行前会估算 token）
uv run echo-voc topics list -a 1 --since 30d

uv run echo-voc ask "最近1个月用户反馈较多的问题是什么"
uv run echo-voc ask "有人抱怨发热或耗电吗？" --account 1
uv run echo-voc report --account 1 --since 30d -o monthly.md   # 从库里生成报告，不重新抓取
```

- **问题库**：每个账号维护一份持续积累的 topic 列表。新主题先用 bge-m3 找相似候选（≥0.85 自动合并），模糊区间交给 LLM 判断是否为同一问题，避免"闪退"和"画质差"被误合并
- **排行类问题**：纯向量 top-k 无法回答"较多"，因此按时间窗 SQL 统计各 topic 提及数，并与上一个同长度窗口对比（新增 / 涨幅）
- **开放问题**：向量相似度 + 时间/情感过滤 + 点赞加权重排
- 回答中的 `[#id]` 引用由程序校验并附上原文与链接；模型引用了不存在的 ID 会给出警告

### 4. 产品记忆与趋势对比

```bash
uv run echo-voc snapshot -a 1 --monthly 6m          # 近 6 个月每月一个快照
uv run echo-voc snapshot -a 3 --by-version          # App Store：每个版本一个快照
uv run echo-voc trend -a 1 --last 3                 # 对比最近 3 个快照
uv run echo-voc recall "闪退问题的历史" -a 3          # 查询 mem0 长期记忆
```

- **快照**（PostgreSQL）：保存每个时期的情感占比、各问题提及数与占比、LLM 摘要，数字精确可对比
- **趋势**：程序计算 🆕 新增 / 📈 上升 / 📉 下降 / ✅ 消失 的问题，LLM 写出阶段对比结论
- **mem0**：快照摘要和趋势结论写入 mem0（同一个 pgvector 库 + bge-m3）；`ask` 回答时会自动检索相关历史记忆。默认原文存储（结论本身已是 LLM 生成的摘要），设置 `ECHO_MEMORY_INFER=true` 可改由 mem0 抽取并合并事实；`ECHO_MEMORY=off` 关闭

## 支持的平台

| 平台       | 链接示例                                                                      | 说明                                                                                              |
| ---------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `appstore` | `https://apps.apple.com/cn/app/wechat/id414478124` 或 `appstore:414478124@cn` | 公开 RSS，每个地区最多 500 条最新评论；RSS 不可用时回退到网页（约 10–20 条）。账号：`apps.apple.com/cn/developer/<name>/id<id>` |
| `youtube`  | `https://www.youtube.com/watch?v=...`、`youtu.be/...`、`/shorts/...`          | 需要 [YouTube Data API v3](https://console.cloud.google.com/apis/credentials) Key。账号：`youtube.com/@handle` 或 `/channel/UC...` |
| `bilibili` | `https://www.bilibili.com/video/BV...` 或 `av...`                             | 评论 + 弹幕。账号：`space.bilibili.com/<mid>`，列出全部视频需要登录 Cookie（`BILIBILI_COOKIE`，含 SESSDATA） |

## 支持的模型

| provider           | 默认模型                  | API Key 环境变量                 |
| ------------------ | ------------------------- | -------------------------------- |
| `deepseek`（默认） | `deepseek-flash`          | `DEEPSEEK_API_KEY`               |
| `openai`           | `gpt-5-mini`              | `OPENAI_API_KEY`                 |
| `anthropic`        | `claude-sonnet-5`         | `ANTHROPIC_API_KEY`              |
| `qwen`             | `qwen-plus`               | `DASHSCOPE_API_KEY`              |
| `moonshot`         | `kimi-latest`             | `MOONSHOT_API_KEY`               |
| `gemini`           | `gemini-2.5-flash`        | `GEMINI_API_KEY`                 |
| `openrouter`       | `deepseek/deepseek-flash` | `OPENROUTER_API_KEY`             |
| `ollama`           | `qwen3`                   | 无需                             |
| `custom`           | 需设置 `ECHO_MODEL`       | `ECHO_API_KEY` + `ECHO_BASE_URL` |

优先级：命令行参数 > `ECHO_*` 环境变量 > 预设。例如 vLLM / LM Studio / 硅基流动等兼容接口：

```bash
ECHO_BASE_URL=http://localhost:8000/v1 ECHO_MODEL=Qwen3-8B \
  uv run echo-voc analyze <url> --provider custom
```

## 作为 MCP Server 使用

实时工具（无需数据库）：

- `list_platforms` — 列出支持的平台
- `fetch_comments(url, limit)` — 抓取原始评论，交给你正在用的 AI 助手自己分析（无需额外模型 Key）
- `analyze_comments(url, limit, provider, model, language, output)` — 用 echo 配置的模型生成完整报告

本地工作区工具：

- `track(url)` / `list_accounts()` / `sync(account, work, since, danmaku)` — 跟踪与增量同步
- `ask_feedback(question, account)` — RAG 问答
- `list_topics(account, since, category)` — 问题排行
- `get_trend(account, labels, last)` / `recall_memory(query, account)` — 趋势对比与长期记忆

在 Claude Code 中使用：

```bash
claude mcp add echo -- uv --directory /path/to/echo run echo-voc mcp
```

Claude Desktop / Cursor 等配置示例：

```json
{
  "mcpServers": {
    "echo": {
      "command": "uv",
      "args": ["--directory", "/path/to/echo", "run", "echo-voc", "mcp"],
      "env": {
        "DEEPSEEK_API_KEY": "sk-...",
        "YOUTUBE_API_KEY": "..."
      }
    }
  }
}
```

然后直接问："帮我看看这个 B 站视频的评论里观众最想要什么内容 https://www.bilibili.com/video/BV..."

## 作为 Python 库使用

```python
import asyncio
from echo_voc import service
from echo_voc.report import render_markdown

async def main():
    data = await service.fetch_comments("https://apps.apple.com/cn/app/id414478124", limit=200)
    report = await service.analyze(data, provider="deepseek", language="Chinese")
    print(render_markdown(report))

asyncio.run(main())
```

## 工作原理

实时分析：

```
URL ─▶ Source（自动识别平台）─▶ 统一 Comment 结构
                                   │
                      按长度分批 ────┤
                                   ▼
          Map：每批并发调用 LLM → 情感标签 + 草稿主题（引用评论编号）
                                   │
          Reduce：LLM 合并同类主题 → 摘要 + 优先级建议
                                   │
          程序根据评论编号计算提及数、挑选高赞原文作为证据
                                   ▼
                        Markdown / JSON 报告
```

本地工作区：

```
账号 URL ─▶ list works（增量）─▶ comments / danmaku（按水位增量、限速、断点续跑）
                                        │
        PostgreSQL + pgvector：accounts · works · feedback · topics · snapshots
                                        │
      ┌──────────────┬──────────────────┼────────────────┬─────────────────────┐
      ▼              ▼                  ▼                ▼                     ▼
 embed（bge-m3）  label（LLM）       report           ask（RAG）          snapshot / trend
 feedback 向量    情感 + 问题库       复用 map-reduce  SQL 计数 + 向量检索   结构化快照 + mem0 记忆
```

## 项目结构

```
src/echo_voc/
├── sources/            # 平台适配器（含账号列表、B 站 WBI 签名与弹幕 protobuf 解析）
├── llm/                # 模型适配器：openai_compat.py anthropic.py + 预设
├── embeddings/         # Ollama / OpenAI 兼容 embedding
├── store/              # SQLAlchemy 模型、Alembic 迁移、仓储与查询过滤
├── memory/             # 快照与趋势对比、mem0 适配
├── analyzer.py         # map-reduce 分析
├── analyzer_danmaku.py # 弹幕热点分析
├── sync.py             # 账号 → 作品 → 评论/弹幕 增量同步
├── label.py            # 增量打标与问题库维护
├── rag.py              # 问答：查询规划 + SQL 排行 + 向量检索
├── offline.py          # 基于数据库的报告
├── workspace.py        # CLI 与 MCP 共用的本地工作区操作
├── report.py / prompts.py
├── web/                # 网页界面：FastAPI 路由、Jinja2 模板、HTMX、ECharts、后台任务
├── cli.py / cli_store.py
└── mcp_server.py
```

## 贡献

### 新增一个平台

1. 在 `src/echo_voc/sources/` 下新建文件，继承 `Source`，实现 `matches()` 和 `fetch()`，返回 `FetchResult`
2. 如需支持账号同步，再实现 `matches_account()`、`resolve_account()`、`resolve_work()`、`iter_works()`（新到旧）和 `iter_comments()`（新到旧）；请求统一走 `self.request()` 以获得限速与重试
3. 在 `sources/__init__.py` 的 `SOURCES` 中注册
4. 在 `tests/` 中用 `respx` 模拟接口添加测试

### 新增一个模型

OpenAI 兼容的服务只需在 `llm/__init__.py` 的 `PROVIDERS` 中加一行预设；其他协议继承 `LLM` 实现 `complete()`。

### 开发

```bash
uv sync --all-extras
docker compose up -d        # 数据库相关测试需要；没有数据库时这些测试会自动跳过
uv run pytest
uv run ruff check src tests && uv run ruff format src tests
```

## Roadmap

- [x] 账号级增量抓取与本地存储（PostgreSQL + pgvector）
- [x] B 站弹幕抓取与热点分析
- [x] RAG 问答、问题库、快照趋势与 mem0 记忆
- [ ] 更多平台：Google Play、小红书、抖音、淘宝/京东商品评价、Reddit、Steam、GitHub Issues、V2EX
- [ ] App Store 完整历史评论（突破 RSS 500 条限制）
- [ ] 多链接对比分析（竞品对比）
- [x] 本地网页界面（FastAPI + HTMX）
- [ ] 报告多语言标题、导出 HTML/PDF 报告
- [ ] 定时任务 + 飞书/Slack/邮件推送

## 注意事项

请遵守各平台的服务条款与 robots 规则，合理控制抓取频率，仅将数据用于产品改进等正当用途，不要收集或公开用户个人信息。echo 入库时只保存作者 ID 的哈希值，不保存用户名、头像等个人信息；`BILIBILI_COOKIE` 等凭据只从本地环境变量读取。

## License

[AGPL-3.0](LICENSE)

网页界面内置的第三方前端资源：[htmx](https://htmx.org)（0BSD）、[Apache ECharts](https://echarts.apache.org)（Apache-2.0）、[Pico CSS](https://picocss.com)（MIT）。
