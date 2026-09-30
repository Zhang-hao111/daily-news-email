# 每日新闻日报 · 周报自动化

RSS 抓取 → DeepSeek AI 筛选/分析 → Markdown 报告 → 邮件推送，可选 Obsidian 归档。支持 GitHub Actions 免服务器定时运行，每天早上把 20 条最重要的新闻送进邮箱，每周一自动加发周报。

## 特性

- **三条并行管道**：科技（14 源：36氪、少数派、爱范儿、Solidot、量子位、IT之家、极客公园、钛媒体、TechCrunch、The Verge、Hacker News、Ars Technica、MIT科技评论、BBC科技）＋ 政治（4 源：BBC World、卫报、半岛电视台、NYT World）＋ 财经（8 源：华尔街见闻、CNBC、MarketWatch、SeekingAlpha、Bloomberg、Yahoo Finance、Investing、经济学人）
- **AI 精选**：每天从约 300 条里挑 20 条（科技 12 / 政治 4 / 财经 4），跨源去重、排除软文、保证领域多样性，取舍完全交给大模型
- **正文级分析**：AI 筛选后并发抓取文章正文（trafilatura 抽取），摘要基于全文而非 RSS 摘要
- **逐条分析 + 重要性评分**：分类 + 2~3 句摘要 + 关键数据 + 简短点评 + 1~10 重要性评分，分类内按分排序，头条真实化
- **跨分类事件去重**：同一事件的多篇报道自动合并为信息量最大的一条
- **AI 今日综述 + 市场快照**：整体趋势概括，外加 A 股三大指数行情（东方财富公开接口）
- **周报**：每天自动积累当日精选，周一自动汇总出上周最重要的 15 条（`--weekly` 可手动触发）
- **过滤链**：新鲜度过滤（默认 36 小时）→ 跨天防重复推送 → 关注/排除关键词
- **推送**：SMTP 邮件（HTML + 纯文本兜底）＋ 可选飞书/钉钉/企业微信群机器人 webhook
- **可选 Obsidian 归档**：存入 `daily-briefing/` 并自动在当天日记挂上 wikilink
- **可观测**：控制台 + `logs/年月.log` 落盘日志，`--dry-run` 干跑模式（完整跑链路但不发邮件不写档）

## 快速开始

```bash
git clone https://github.com/Zhang-hao111/daily-news-email.git
cd daily-news-email
pip install -r requirements.txt
cp .env.example .env   # 填入你的配置
python send_email.py --dry-run   # 干跑验证（调 AI，但不发邮件/不写档）
```

### 配置（.env）

| 变量 | 必填 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | ✅ | DeepSeek API Key（[platform.deepseek.com](https://platform.deepseek.com)） |
| `SMTP_HOST` / `SMTP_PORT` | ✅ | SMTP 服务器，如 `smtp.qq.com` / `465`（SSL）或 `smtp.gmail.com` / `587`（STARTTLS） |
| `SMTP_USER` / `SMTP_AUTH_CODE` | ✅ | 发件邮箱 + 授权码（QQ 邮箱用授权码，Gmail 用应用专用密码） |
| `EMAIL_TO` | ✅ | 收件人，多个用英文逗号分隔 |
| `OBSIDIAN_VAULT_PATH` | 可选 | Obsidian 库路径，不配置则跳过存档 |
| `FOCUS_KEYWORDS` | 可选 | 关注关键词，标题命中的文章 AI 筛选时优先 |
| `EXCLUDE_KEYWORDS` | 可选 | 排除关键词，标题命中即过滤（拦截软文/广告） |
| `NEWS_MAX_AGE_HOURS` | 可选 | 新鲜度窗口，默认 36 小时 |
| `PUSH_WEBHOOK_URL` / `PUSH_WEBHOOK_TYPE` | 可选 | 群机器人推送（`feishu`/`dingtalk`/`wecom`），留空不推送 |

### 云端部署（GitHub Actions，推荐）

1. Fork 本仓库（或推到自己的新仓库）
2. 仓库 Settings → Secrets and variables → Actions，添加上表中的 Secrets（至少 DeepSeek 和 SMTP 五项）
3. 完成 —— `.github/workflows/daily.yml` 每天 UTC 01:05（北京时间 09:05）自动运行，也可在 Actions 页手动触发

云端不需要配置 `OBSIDIAN_VAULT_PATH`（自动跳过存档）；`state/` 目录（防重复记录 + 周报积累）通过 Actions Cache 跨运行持久化。

> 注意：GitHub 会在仓库 60 天无提交后停用定时工作流，偶尔推个提交即可保活。

## 本地定时（Windows）

以管理员身份运行 `setup_task.bat`，自动探测 Python 路径并注册每天 09:05 的计划任务（电池供电也会运行、错过的任务开机补跑）。与云端部署二选一，避免重复邮件。

## 常用命令

```bash
python send_email.py             # 正式运行：抓取→筛选→分析→报告→推送
python send_email.py --dry-run   # 干跑：真实调 AI 生成报告存到 logs/，但不推送
python send_email.py --weekly    # 手动生成上周周报（平时周一自动）
```

## 架构

单文件 `send_email.py`，`PIPELINE` 配置字典定义各管道差异，加一类新闻只需加一项配置：

```
抓取（并发）→ 去重 → 新鲜度/防重/关键词过滤 → AI 筛选 → AI 分析（并发，带重试）
    → AI 今日综述 → Markdown 报告 → Obsidian（可选）→ 邮件/Webhook → 记录防重复
    → 周积累 → 周一自动汇总周报
```

新闻均来自各站公开 RSS；AI 生成的摘要与点评可能存在偏差，重要信息请以原文为准。
