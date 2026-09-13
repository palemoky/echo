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
- **三种用法**：命令行、Python 库、MCP Server（接入 Claude Desktop / Cursor / Cherry Studio 等）

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
uv run echo-voc analyze "https://www.bilibili.com/video/BV1GJ411x7h7" \
  --provider anthropic --lang Chinese -n 300 -o report.md

# 分析 YouTube 评论（需要 YOUTUBE_API_KEY），用本地 Ollama 模型
uv run echo-voc analyze "https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
  --provider ollama --model qwen3

# 只抓取评论（不调用模型），之后再分析
uv run echo-voc fetch "https://www.bilibili.com/video/BV1GJ411x7h7" -o comments.json
uv run echo-voc analyze comments.json -f json -o report.json
```

其他命令：`echo-voc sources`（支持的平台）、`echo-voc providers`（模型预设）、`echo-voc --help`。

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

## 支持的平台

| 平台       | 链接示例                                                                      | 说明                                                                                              |
| ---------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `appstore` | `https://apps.apple.com/cn/app/wechat/id414478124` 或 `appstore:414478124@cn` | 公开 RSS，每个地区最多 500 条最新评论；RSS 不可用时回退到网页（约 10–20 条）                      |
| `youtube`  | `https://www.youtube.com/watch?v=...`、`youtu.be/...`、`/shorts/...`          | 需要 [YouTube Data API v3](https://console.cloud.google.com/apis/credentials) Key（免费额度足够） |
| `bilibili` | `https://www.bilibili.com/video/BV...` 或 `av...`                             | 按热度排序；若被风控（-352）可设置 `BILIBILI_COOKIE`                                              |

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

echo 提供 3 个工具：

- `list_platforms` — 列出支持的平台
- `fetch_comments(url, limit)` — 抓取原始评论，交给你正在用的 AI 助手自己分析（无需额外模型 Key）
- `analyze_comments(url, limit, provider, model, language, output)` — 用 echo 配置的模型生成完整报告

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

## 项目结构

```
src/echo_voc/
├── sources/        # 平台适配器：appstore.py youtube.py bilibili.py
├── llm/            # 模型适配器：openai_compat.py anthropic.py + 预设
├── analyzer.py     # map-reduce 分析
├── prompts.py      # 提示词
├── report.py       # Markdown 渲染
├── service.py      # CLI 与 MCP 共用的入口
├── cli.py          # 命令行
└── mcp_server.py   # MCP Server
```

## 贡献

### 新增一个平台

1. 在 `src/echo_voc/sources/` 下新建文件，继承 `Source`，实现 `matches()` 和 `fetch()`，返回 `FetchResult`
2. 在 `sources/__init__.py` 的 `SOURCES` 中注册
3. 在 `tests/test_sources.py` 中用 `respx` 模拟接口添加测试

### 新增一个模型

OpenAI 兼容的服务只需在 `llm/__init__.py` 的 `PROVIDERS` 中加一行预设；其他协议继承 `LLM` 实现 `complete()`。

### 开发

```bash
uv sync
uv run pytest
uv run ruff check src tests && uv run ruff format src tests
```

## Roadmap

- [ ] 更多平台：Google Play、小红书、抖音、淘宝/京东商品评价、Reddit、Steam、GitHub Issues
- [ ] App Store 完整历史评论（突破 RSS 500 条限制）
- [ ] 增量抓取与本地存储（SQLite），追踪版本间反馈变化
- [ ] 多链接对比分析（竞品对比）
- [ ] 报告多语言标题、HTML 报告与 Web UI
- [ ] 定时任务 + 飞书/Slack/邮件推送

## 注意事项

请遵守各平台的服务条款与 robots 规则，合理控制抓取频率，仅将数据用于产品改进等正当用途，不要收集或公开用户个人信息。

## License

[AGPL-3.0](LICENSE)
