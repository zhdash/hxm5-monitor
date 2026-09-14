# 线报屋监控 → 企业微信推送

每 **60 秒**抓取一次 `https://www.hxm5.com/xianbao/3/`（热门活动），发现新帖立即把
**标题 + 正文全文 + 图片链接 + 全部回复** 推送到企业微信群「线报推送」。

## 运行位置（重要）

监控**跑在腾讯云轻量服务器上**，本机不再运行 —— 两边同时跑会重复推送。

| 项目 | 值 |
|---|---|
| 服务器 | 广州 · `114.132.102.43`（实例 `lhins-p91l961t`，Ubuntu 24.04 / Python 3.12） |
| 程序目录 | `/opt/hxm5/` |
| 定时任务 | 用户级 crontab，**每分钟**执行 `server_monitor.py --once` |
| 日志 / 存档 | `/opt/hxm5/monitor.log` · `/opt/hxm5/archive/` |

服务器端用的是 `server_monitor.py`（**cron 版**：单次执行即退出，不做常驻进程，重启自动恢复）。
本机版 `hxm5_monitor.py` 保留备用。

SSH 登录后常用命令：

```bash
tail -20 /opt/hxm5/monitor.log                              # 看日志
/usr/bin/python3 /opt/hxm5/server_monitor.py --test         # 测推送
/usr/bin/python3 /opt/hxm5/server_monitor.py --backfill 3   # 补推最近 3 条
crontab -l                                                  # 看定时任务
crontab -l | grep -v hxm5 | crontab -                       # 停用（保留其他任务）
```

## 推送内容与渠道

一条新帖会推三样东西：

1. **企微文字卡片**（markdown）—— 标题 + 正文 + 回复 + 原帖链接
2. **企微图片** —— 帖子配图用 `image` 类型真发原图，每帖最多 `max_images_per_post`（默认 3）张
3. **Bark 通知**（iOS）—— 标题 + 正文摘要，`icon` 用首图，点击跳原帖

> 企微的 markdown 消息**不支持内嵌图片**，这是官方限制，所以图片走独立的 `image` 类型消息
> （base64 + md5，编码前需 ≤ 2MB）。

渠道开关都在 `config.json`：`wecom_webhook` 留空则不推企微，`bark_key` 留空则不推 Bark。

**企微限速 20 条/分钟**，程序内置滑动窗口限速（18 条/分钟）自动排队。
如果一条帖子图很多、又频繁更新，可能触发排队等待 —— 此时可调小 `max_images_per_post`。

## 文件说明

| 文件 | 作用 |
|---|---|
| `hxm5_monitor.py` | 主程序（纯标准库，无第三方依赖） |
| `start_monitor.py` | 后台启动器（脱离终端独立运行） |
| `config.json` | 配置：板块、间隔、Webhook 等 |
| `state.json` | 已推送帖子 ID（去重，重启不重复推） |
| `monitor.log` | 运行日志（自动轮转，保留 3 份 5MB） |
| `archive/日期.jsonl` | 帖子原文存档，每行一条完整 JSON |
| `1-start.bat` / `2-stop.bat` | 双击启动 / 停止 |

## 本机启动（备用，当前未启用）

上云后本机的开机自启项已移除、进程已停止，避免与服务器重复推送。
若服务器不可用、想改回本机运行：

- **启动**：双击 `1-start.bat`
- **停止**：双击 `2-stop.bat`
- **恢复开机自启**：在启动文件夹（`Win+R` 输入 `shell:startup`）新建一个 vbs，
  内容为 `Set ws = CreateObject("WScript.Shell")` 换行后加
  `ws.Run """C:\Users\jones\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"" """ & 本目录 & "\hxm5_monitor.py""", 0, False`
  （注：**必须存成 ANSI/GBK 编码**，因为路径含中文）

程序内置**单实例保护**：重复启动时会检测到已有进程并直接退出。

**排查**：`python hxm5_monitor.py --status` 看是否在跑（以进程真实存活为准，不是只看 pid 文件）；
没在跑就双击 `1-start.bat`。日志在 `monitor.log`。

## 常用操作

```bash
python hxm5_monitor.py --status      # 查看运行状态
python hxm5_monitor.py --test        # 发一条测试消息
python hxm5_monitor.py --backfill 5  # 补推最近 5 条（验证效果）
python hxm5_monitor.py --dry         # 单轮演练，不推送
python hxm5_monitor.py               # 前台常驻（调试用）
```

停止：双击 `2-stop.bat`，或 `taskkill /F /PID <monitor.pid 里的数字>`

## 配置项（config.json）

| 字段 | 说明 |
|---|---|
| `cid` | 板块 ID：`3`=热门活动，`0`=最新线报（更新最快最全） |
| `interval_sec` | 轮询间隔，默认 60 秒 |
| `wecom_webhook` | 企业微信「消息推送」Webhook 地址 |
| `max_posts_per_cycle` | 单轮最多处理几条新帖 |
| `push_on_first_run` | 首次运行是否推送存量帖（默认 false，只建基线） |
| `reply_limit` | 每条帖子最多附带多少条回复 |

改完配置**需要重启才生效**。

## 技术要点（以后维护用）

站点是 JS 动态渲染的 SPA，静态 HTML 没有帖子数据，走的是自研 JSON 接口，
并且**带防爬签名**，签名算法逆向自 `/ui/app.js`：

```
jt = floor(Date.now() / 180000)                    # 5 分钟一档的时间片
jx = FNV1a32("<path>|<querystring>|hxm5-json-guard-v1") 然后转 36 进制
```

请求要求：`POST`、`Content-Type: application/x-www-form-urlencoded`、
必须带 `X-HXM5-JSON: 1` 和 `X-Requested-With: XMLHttpRequest`。

- 列表：`/xianbao/{cid}/json/`，参数 `r=list`
- 详情：`/t/{id}/json/`，参数 `r=thread&id={id}`
- 返回统一为 `{"code":200,"data":{...}}`

**若失效**：多半是站点改了盐值或算法，重新拉 `/ui/app.js` 搜 `JSON_GUARD_SALT`
和 `jsonGuardHash` 即可，改 `hxm5_monitor.py` 顶部的 `SALT` 常量。

## 注意事项

- **已配置开机自启**（启动文件夹里的 `hxm5_monitor.vbs`），开机自动运行，不需要手动启动。
  重启不会重复推送已推过的帖子；关机期间出现的新帖会在下次启动时补推（按 `max_posts_per_cycle` 分批）。
- 企业微信「消息推送」限速 **20 条/分钟**，程序内置限速 18 条/分钟并自动排队。
- 企微 markdown **不支持内嵌图片**，帖子配图以原图链接形式给出，点开可看。
- 群聊若开启「全员禁言」，推送会失败。
