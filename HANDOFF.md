# 交接文档 — Scrapper Social Media

**项目目的**：本机跑的单页工具，用 Apify 按关键词发现 TikTok / Instagram 博主、过滤合格创作者、Gemini AI 评估、一键起草并通过 Gmail 直发外联邮件。

**仓库**：`git@github.com:liuyika3/scrapper-social-media.git`（HTTPS：`https://github.com/liuyika3/scrapper-social-media.git`）

**项目根**：`C:\Users\2025lyk\Desktop\scrapper-social-media\`

---

## 0. 一句话快速验收

```
cd C:\Users\2025lyk\Desktop\scrapper-social-media
python tools\creator_screener_server.py
```
浏览器开 `http://127.0.0.1:5180/` → 顶部 chip 显示 "📧 未授权" → 点 🔑 用 Gmail 授权走 OAuth → 填 Apify Token + 关键词 → 开始。

---

## 1. 文件结构

```
scrapper-social-media/
├── README.md                            ← 用户视角说明
├── HANDOFF.md                           ← 你正在看
├── .gitignore                           ← 不进 commit 的本地文件
├── start_creator_screener.bat           ← Windows 启动器（ASCII + CRLF）
├── 一键启动-达人筛选.bat                  ← 转发到上面
├── tools/
│   ├── apify_creator_screener.py        ← Apify 调用 / 双平台 / Gemini / 评论 / 视频去重
│   ├── creator_screener_server.py       ← stdlib HTTPServer，所有 /api 路由
│   └── creator_screener_index.html      ← 单文件 dark UI
└── (运行时生成，gitignore)
    ├── .google-accounts.json            ← Gmail OAuth client + token
    ├── .favorites.json                  ← 收藏库（永久）
    └── .seen-creators.json              ← 已查过的 handle 列表（去重用）
```

**纯标准库**：无 `requirements.txt`、无 npm。Python 3.10+ 即可。

---

## 2. 用户需要的三个 Key（放 UI 里，不写磁盘 = OAuth/SMTP 除外）

| Key | 用途 | 哪里拿 | 存在哪 |
|---|---|---|---|
| Apify Token | 跑 TT/IG 爬虫 | https://console.apify.com/account/integrations | 浏览器 localStorage |
| Gemini API Key | 🤖 AI 触达评估 | https://aistudio.google.com/apikey | 浏览器 localStorage |
| Gmail OAuth Client | 直接发邮件 | Google Cloud Console（详见 §6） | `.google-accounts.json`（含 refresh token，本地文件） |

---

## 3. API 端点（都在 `127.0.0.1:5180`）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/` | 单文件 UI |
| GET | `/api/health` | 健康检查 |
| GET | `/api/auth/status` | 看 OAuth 是否已授权 + 当前 active 账号 |
| POST | `/api/auth/config` | 保存 clientId + secret + email，返回 Google authUrl |
| POST | `/api/auth/logout` | 清掉 token（保留 client config） |
| GET | `/oauth/callback?code&state` | OAuth 回调，换取 token 并保存 |
| POST | `/api/screen` | 跑一次筛选 / 发现 |
| POST | `/api/comments` | 手动追拉评论（按需） |
| POST | `/api/ai-recommend` | Gemini 评估单个博主 |
| POST | `/api/send-mail` | Gmail API 直发（OAuth 路径），SMTP App Password 兜底 |
| GET | `/api/favorites` | 列出全部收藏 |
| POST | `/api/favorites/upsert` | 添加 / 更新一个收藏 |
| POST | `/api/favorites/remove` | 移除 |
| POST | `/api/favorites/status` | 切换 已联系 / 未联系 |
| POST | `/api/favorites/save-ai` | 把 AI 建议挂到收藏记录上 |
| GET | `/api/seen` | 已查过的博主总数 + 列表（去重用） |
| POST | `/api/seen/clear` | 清空已查记录 |
| POST | `/api/seen/forget` | 单独"忘掉"一个 handle |
| GET | `/api/img-proxy?url=` | IG 封面/头像 CDN 代理（绕 referer） |

**`/api/screen` 入参（核心字段）**：
```json
{
  "apifyToken": "...",
  "platform": "tiktok" | "instagram",
  "keywords": "fitness, gym",      // 发现模式
  "handles": "alex\nbob",          // 直接给定模式（与 keywords 二选一）
  "minFollowers": 1000,
  "days": 30,                       // 统计窗口
  "threshold": "tiktok" | "other",  // 均播 > 粉丝 OR > 半粉丝
  "discoverLimit": 30,              // 最多发现 N 个
  "dedupe": true,                   // 跳过已查过的
  "fetchTopComments": true          // 默认抓 top 视频的 top 评论
}
```

**返回**：
```json
{
  "ok": true,
  "rows": [/* analyze_profile 输出，TT/IG 同 schema */],
  "discovery": { "keywords":[..], "raw_count":60, "kept":30, "skipped_seen":15, "search_actor":"..." },
  "seenCount": 87
}
```

每 row 字段：
```
username, nickname, verified, region, avatar, profile_url
followers, following, total_hearts, total_videos_lifetime
bio, posts_in_window, avg_play_in_window, required_avg_for_rule
pass_min_followers, pass_view_rule, pass_all
last_post_at, keyword_matched
recent_videos_top: [{cover, plays, likes, comments, url, caption, ts_iso}]
top_comments: [{text, likes, user, user_nick, user_avatar, from_video_url, from_video_plays}]   // TT only
contacts: {emails[], instagram[], youtube[], links?[]}
platform: "tiktok" | "instagram"
```

---

## 4. Apify Actor 默认配置

| 用途 | Actor | 关键 input |
|---|---|---|
| TT 主页 | `clockworks/tiktok-profile-scraper` | `profiles[]` |
| TT 关键词搜索 | `clockworks/tiktok-scraper` | `searchQueries[]` |
| TT 视频评论 | `clockworks/tiktok-comments-scraper` | `postURLs[]`, `commentsPerPost` |
| IG 主页 | `apify/instagram-profile-scraper` | `usernames[]` |
| IG hashtag 搜索 | `apify/instagram-scraper` | `directUrls=[https://www.instagram.com/explore/tags/<kw>/]` + `searchTerm` 兜底 |

UI **高级** 折叠面板里都能覆盖默认 actor。

---

## 5. 本地存储（全在项目根）

| 文件 | 内容 | 何时写 |
|---|---|---|
| `.google-accounts.json` | OAuth `{activeAccount, accounts:{email:{clientId, clientSecret, token:{access_token, refresh_token, expires_at}}}}` | OAuth 流程 |
| `.favorites.json` | `{username_lower: {creator{}, status, addedAt, lastContactedAt, aiAdvice, aiSavedAt}}` | 加/改收藏、保存 AI |
| `.seen-creators.json` | `{username_lower: {firstSeen, platform}}` | 每次 /api/screen 完成 |

**所有文件都在 `.gitignore`**。换电脑只要把这 3 个文件拷过去，所有"已联系/已查/AI 报告"全部跟随。

---

## 6. Gmail OAuth 一次性配置（最容易卡住的步骤）

按 `mail-tool-gmail/GMAIL配置全流程.md` 的流程，**端口换成 5180**：

1. https://console.cloud.google.com/ → 启用 Gmail API
2. OAuth consent screen → User type: External → 加 Test users（要发件那个 Gmail，比如 jovidasmith@gmail.com）
3. Credentials → Create credentials → OAuth client ID → Web application → **Authorized redirect URIs** 必须包含：
   ```
   http://127.0.0.1:5180/oauth/callback
   http://localhost:5180/oauth/callback
   ```
4. UI 顶部 🔑 用 Gmail 授权 → 填邮箱 + Client ID + Secret → 去 Google 授权 →
   - 我们的 OAuth URL 已加 `prompt=select_account consent` + `login_hint`，强制弹账号选择器，避免选到错的账号
   - 选中 **在 Test users 里的那个 Gmail**（不是默认登录的别的）

**常见错误**：
- "请求无效 / redirect_uri_mismatch" → step 3 的 redirect URI 没加
- "禁止访问" + 你选的不是 Test users 里那个 → 重新走，选对账号
- token 失效 → UI chip 转黄 → 点 🔑 按钮 → 在 modal 里点登出 → 重新走 OAuth

---

## 7. 启动守卫与坑

`creator_screener_server.py` 启动时通过 `inspect.getsource(apify_run_actor)` 检查源码，发现旧的 `wait-for-finish?` URL 模式立刻 `SystemExit(2)`（避免回归 Apify 已下线 endpoint）。

**编码政策**（很重要，HANDOFF 必读）：
- `*.py` / `*.html` 永远 **UTF-8 无 BOM**
- `*.bat` 永远 **ASCII + CRLF**（中文文案放 Python print / HTML 里，不要进 bat）
- Cursor / VSCode 偶尔会把 HTML 改成 UTF-16 LE 或 把 Python 文件回退到老版本——出现这种情况：
  ```cmd
  copy C:\Users\2025lyk\Desktop\scrapper-social-media\tools\*.* <项目>\tools\
  ```
  从 git 仓库直接覆盖工作目录

---

## 8. 已知限制

- IG 视频下载未实装（之前 TT 路径效果不好已下掉）
- IG 评论暂未抓（需要切到 `apify/instagram-comment-scraper`，未做）
- 飞书 webhook 通知未做
- 没有多用户 / 多账号 namespace（单机单用户工具）
- 没有自动化测试套件

---

## 9. 给 agent 接手的提示

- 改代码 **只在 `C:\Users\2025lyk\Desktop\scrapper-social-media\` 这个独立 git 仓库里改**。原工作目录 `C:\Users\2025lyk\Desktop\dmoes\万物教练api demos\tools\` 经常被 Cursor 反向 sync 回老版本，是个流沙。
- push 用 SSH（GitHub Desktop / Cursor 终端 / git bash 任何一个能 push 的环境）。本机 Windows ACL 经常让命令行 ssh 拒绝读私钥；Cursor 内嵌终端通常没问题。
- 用户的 Apify Token / Gemini Key / 各种 secret **从不进 git**——前 2 个在 localStorage、第 3 个在 `.google-accounts.json`（已 gitignore）。
- 改 prompt：`apify_creator_screener.py` 顶部的 `DEFAULT_AI_PROMPT_TEMPLATE`。
- 用户也能在 UI 顶部 **📝 AI 提示词** 自定义，那一份存浏览器 localStorage（`aiUserContext` + `aiPromptTemplate`）。
