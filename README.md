# Scrapper Social Media — TikTok / Instagram 达人发现 + 外联工作台

本机跑的单页工具：用 **Apify** 按关键词发现 TikTok / Instagram 博主，过滤合格创作者，**Gemini** 给报价与外联建议，一键起草并通过 **Gmail SMTP 直发**外联邮件，全程不离开当前页面。

---

## 一分钟看懂

- 双击 `start_creator_screener.bat` → 浏览器自动打开 `http://127.0.0.1:5180/`
- 顶部切平台：🎵 TikTok / 📸 Instagram
- 填好关键词（默认走"发现"模式，也可直接给定 handle）→ 点【开始】
- 出博主卡片：头像、粉丝、均播、最近发布、活跃徽章、Top 视频缩略（可下载内嵌播放）、bio 抽出的邮箱/IG/YT
- 每张卡上的按钮：
  - **✉ 起草邮件** — 弹模态，预填模板（变量替换 `{{name}}` `{{username}}` `{{followers}}` 等），点发送 → 通过本机 Gmail SMTP 直发，自动收藏 + 标"已联系"
  - **🤖 AI** — Gemini 评估是否触达 + 三档报价（post/story/co-created）+ 外联话题 + 警示
  - **💬 评论** — 抓该博主热门视频的 Top 评论
  - **★** — 加入收藏目录
- 顶部右上 chip 显示 SMTP 配置状态；"✏ 邮件模板" 可改默认 draft（保存到 localStorage）
- seg 切换：卡片 / 表格 / **收藏(N)**

---

## 快速开始（人）

### 1. 装 Python

需要 Python 3.10+。`python` 或 `py` 在 PATH 即可。

### 2. 拿三个 Key

| Key | 用途 | 哪里拿 |
|---|---|---|
| Apify Token | 调 TikTok / IG actor | https://console.apify.com/account/integrations |
| Gemini API Key | AI 评估（可选） | https://aistudio.google.com/apikey |
| Gmail App Password | SMTP 发邮件 | 先开两步验证，然后 https://myaccount.google.com/apppasswords |

### 3. 启动

```cmd
start_creator_screener.bat
```

或 `python tools\creator_screener_server.py`

### 4. 在 UI 配置

- **Apify Token** 字段填 token
- 展开 **高级** 折叠面板：
  - **发件 Gmail 地址** — `you@gmail.com`
  - **Gmail App Password** — 16 位的 app password
  - **发件人显示名**（可选） — 比如 `Jovida Team`
  - **Gemini API Key**（可选） — 启用 🤖 AI 按钮
- 全部存 localStorage，不写磁盘，不提交 git

---

## 项目结构（agent）

```
scrapper-social-media/
├── README.md                           ← 你正在看
├── start_creator_screener.bat          ← 启动器（ASCII + CRLF）
├── 一键启动-达人筛选.bat                  ← 转发到上面那个
└── tools/
    ├── apify_creator_screener.py       ← Apify 调用、IG/TT 双平台、Gemini、视频下载
    ├── creator_screener_server.py      ← stdlib HTTPServer，所有 /api 路由
    └── creator_screener_index.html     ← 单文件 UI（dark theme，cards / table / favs / mail modal / template editor）
```

无 `requirements.txt`：纯 Python 标准库（`urllib`, `http.server`, `smtplib`, `email`），开箱即用。

---

## API 路由（都在 `127.0.0.1:5180`）

| 方法 | 路径 | 用途 | 关键字段 |
|---|---|---|---|
| GET | `/` | UI | — |
| GET | `/api/health` | 健康检查 | — |
| POST | `/api/screen` | 跑一次筛选/发现 | `apifyToken`, `platform: tiktok\|instagram`, `keywords`, `handles`, `minFollowers`, `days`, `threshold`, `discoverLimit`, `downloadTopVideo` |
| POST | `/api/comments` | 抓视频 Top 评论 | `apifyToken`, `videoUrls[]` |
| POST | `/api/ai-recommend` | Gemini 评估单个博主 | `geminiKey`, `model`, `creator{}` |
| POST | `/api/send-mail` | Gmail SMTP 直发 | `senderEmail`, `appPassword`, `recipients`, `subject`, `body`, `plainText` |

请求 / 响应都是 JSON。`/api/screen` 返回 `{ok, rows[], discovery, count}`，每个 row 是 `analyze_profile` 的输出（platform 无关字段）：

```js
{
  username, nickname, verified, region, avatar, profile_url,
  followers, following, total_hearts, total_videos_lifetime,
  bio, posts_in_window, avg_play_in_window, required_avg_for_rule,
  pass_min_followers, pass_view_rule, pass_all,
  last_post_at, keyword_matched,
  recent_videos_top: [{cover, plays, likes, comments, url, caption, ts_iso}, ...],
  contacts: {emails[], instagram[], youtube[], links?[]},
  top_video_mp4?: "https://...mp4",   // 仅当勾选下载 Top 视频
  platform: "tiktok" | "instagram"
}
```

---

## Apify Actor 默认配置

写在 `apify_creator_screener.py` 顶部，可在 UI "高级" 里覆盖：

| Actor | 用途 | 输入字段 |
|---|---|---|
| `clockworks/tiktok-profile-scraper` | TT 主页画像 | `profiles[]` |
| `clockworks/tiktok-scraper` | TT 关键词搜索 / 视频下载 | `searchQueries[]` 或 `videos[]` + `shouldDownloadVideos` |
| `clockworks/tiktok-comments-scraper` | TT 视频评论 | `postURLs[]` |
| `apify/instagram-profile-scraper` | IG 主页画像 | `usernames[]` |
| `apify/instagram-scraper` | IG hashtag 搜索 | `search`, `searchType: hashtag` |

---

## Gmail SMTP 直发（重要）

不走 OAuth、不依赖外部进程。流程：

1. 用户在 UI 填发件 Gmail + 16 位 App Password（去掉空格也行）
2. `/api/send-mail` 在本机用 `smtplib.SMTP_SSL("smtp.gmail.com", 465)` 登录
3. 用 `email.message.EmailMessage` 构 MIME（HTML + 纯文本备选）
4. 每个收件人单独 `send_message`，记录 message-id 与失败原因
5. 返回 `{ok, results[], sent, failed}`

**为什么不用 OAuth**：refresh_token 会被 Google 静默吊销；`/api/auth/status` 只看文件存在，不验真。SMTP 直发用 App Password 永不过期，调试体验线性。

**前提**：Gmail 账号必须开启**两步验证**才能在 https://myaccount.google.com/apppasswords 生成 App Password。Less-secure-apps 已被 Google 全面下线。

---

## Gemini AI 评估

模型：默认 `gemini-2.5-flash`（在 UI 改）。Prompt 强制返回结构化 JSON：

```json
{
  "recommend_outreach": "yes|maybe|no",
  "recommend_reason": "...",
  "suggested_price_usd": {"post": 0, "story": 0, "video_collab": 0},
  "price_reason": "...",
  "talking_points": ["...", "..."],
  "warnings": ["..."]
}
```

输入是 creator 的关键画像（粉丝、均播、活跃度、bio、recent_captions），不传完整视频列表节省 token。一次调用约 $0.0005。

---

## 收藏 + 标签

- localStorage key：`favCreators_v1`
- 结构：`{username_lower: {creator, status: 'uncontacted'|'contacted', addedAt, lastContactedAt}}`
- 状态切换在卡片顶部 pill；移除靠卡片右下角 ✕
- 邮件发送成功后**自动**收藏 + 标"已联系"

---

## 启动守卫

`creator_screener_server.py` 启动时 `inspect.getsource(apify_run_actor)` 检查源码，发现旧 URL 模式 `wait-for-finish?` 立刻 `SystemExit(2)`，避免回归到 Apify 已下线的 endpoint。

---

## 常见问题

| 症状 | 原因 / 解 |
|---|---|
| `📧 未配置发件邮箱` chip 是黄色 | 高级里没填 senderEmail + appPassword |
| `Gmail rejected login: ...` | 没用 App Password（用了主密码 / 没开两步验证） |
| `wait-for-finish?` 守卫触发 | tools/apify_creator_screener.py 是老版本，按 README 重置 |
| HTML 页面中文乱码 | 文件被改成 UTF-16 了。把 `tools/creator_screener_index.html` 重存为 UTF-8 无 BOM |
| `start_creator_screener.bat` 出现 `'xxx' 不是内部或外部命令` | bat 不是 ASCII + CRLF。重存为 ASCII + CRLF。**HANDOFF 政策：bat 永远 ASCII**，中文文案放进 Python print 或 HTML |

---

## 许可

私人使用，未做开源声明。

## Credits

`Anthropic Claude` 配 user `lyk` 共建。
