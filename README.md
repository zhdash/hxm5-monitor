# 线报屋监控 → 钉钉推送

抓取 `https://www.hxm5.com/xianbao/3/`（热门活动），发现新帖立即把
**标题 + 正文 + 图片 + 回复 + 原帖链接** 推到钉钉群。

> **本文档最后校订：2026-10-13**。此前版本描述的是"服务器每分钟跑 + 推企微 + 本机开机自启"，
> 那套架构已经不存在（服务器 2026-09-22 销毁、企微 2026-09-24 关闭），已重写。

---

## 一、现在是怎么跑的

**唯一运行位置：GitHub Actions**（仓库 `zhdash/hxm5-monitor`，**public**）。

```yaml
on:
  schedule:      - cron: '*/5 * * * *'
  workflow_dispatch:
timeout-minutes: 355        # job 最长 5 小时 55 分
concurrency:  group: hxm5-monitor, cancel-in-progress: false
```

关键在于**「一次触发，job 内长循环」**：

```
被触发一次 ──► 连续盯约 340 分钟，每 5 分钟扫一轮 ──► 循环结束
```

**为什么要长循环**：GitHub 的 `schedule` 是 best-effort，自 2026-08-26 起出现平台级故障。
实测 60 次运行的间隔是 **最短 105 / 中位 205 / 最长 413 分钟**（理论应每 5 分钟一次）。
社区讨论：<https://github.com/orgs/community/discussions/206019>

所以**不再指望 cron 的频率**，只要被触发一次就把这几小时盯满。
想更密就配外部调度器打 `workflow_dispatch`（见 `HEARTBEAT_SETUP.md`）。

### 渠道现状

| 渠道 | 状态 |
|---|---|
| **钉钉** | ✅ **唯一启用**（加签模式） |
| 企业微信 | ⛔ 2026-09-24 按需求关闭（workflow 里已注释，取消注释即可恢复） |
| Bark (iOS) | ⚪ 未启用 |

---

## 二、文件结构

```
actions_monitor.py          ⭐ 唯一活的实现（GitHub Actions 版）
test_hxm5.py                回归测试（61 项断言，含端到端）
.github/workflows/monitor.yml   线上入口：长循环 + 状态提交
state/hxm5_state.json       去重状态（由 workflow 每轮提交回仓库）
deploy_actions.py           最初的建仓/部署工具
ACTIONS_DEPLOY.md           部署历史与实测记录
HEARTBEAT_SETUP.md          外部心跳（cron-job.org）操作单
CODE_REVIEW.md              2026-10-13 代码审查报告
legacy/                     ⛔ 已停用的历史实现（服务器版 / SCF 版 / 本机版），仅存档
```

> ⚠️ **改动行为时只改 `actions_monitor.py`**。
> `legacy/` 里还有三份几乎同源的 monitor（与 actions 版重复率高达 80.1%），
> 历史上正是「改一处漏一处」导致了 2026-09-24「一条都不推」的事故（详见 `legacy/README.md`）。

---

## 三、配置（全部走 GitHub Secrets）

环境变量名 = 仓库 Secret 名带 `HXM5_` 前缀。未配置项有脚本内默认值兜底。

| Secret | 默认 | 说明 |
|---|---|---|
| `HXM5_DINGTALK_WEBHOOK` | — | **必填**。留空则脚本判定"没配渠道"，直接以退出码 2 失败 |
| `HXM5_DINGTALK_SECRET` | — | 填了走**加签**模式；留空走关键词模式 |
| `HXM5_CID` | `3` | 板块：`3`=热门活动，`0`=最新线报 |
| `HXM5_MAX_POSTS_PER_CYCLE` | `12` | 单轮最多处理几条新帖 |
| `HXM5_REPLY_LIMIT` | `15` | 每帖最多附带多少条回复 |
| `HXM5_PUSH_IMAGES` | `false` | 是否额外发原图（企微专用，钉钉图片是内嵌的） |
| `HXM5_WECOM_WEBHOOK` | — | 已注释；恢复企微时再启用 |

本地跑测试时用同名环境变量即可。

---

## 四、去重与重试（重要）

去重是**两层**的，缺一层都会出问题。

```json
{
  "seen":    ["2935931", ...],        // ① 已送达的帖子 ID
  "pending": { "2935902": 2 },        // 已发现但没送出去的，值 = 已失败次数
  "sent_fp": { "a1b2c3d4e5f60718": 1760800000 }   // ② 已推送过的【内容指纹】→ 时间戳
}
```

### 第一层：帖子 ID（`seen`）

- 有推送成功（或重试到上限放弃）才写进 `seen`
- 推送失败 → 记进 `pending`，**下一轮优先补发**，最多试 `MAX_PUSH_RETRY = 3` 次
- 补发**不占用**本轮新帖配额
- `seen` 保留最近 `SEEN_KEEP = 5000` 条

> 以前 `seen` 是无条件追加的 → 任何一次瞬时故障都会把帖子永久标记为"已处理"，
> **再也不会重试 = 通知永久丢失**。2026-10-13 已修。

### 第二层：内容指纹（`sent_fp`）

**只靠 ID 去重是不够的**。`sent_fp` 记录"这个内容推过没有"，与帖子 ID 无关：

- 指纹 = `sha1(规范化(标题+正文))[:16]`，**不含 ID / 发帖人 / 时间**
- 同一内容在 `FP_TTL_SEC = 7 天` 内**只推一次**
- 内容太短（`FP_MIN_CHARS = 8` 字以下）不算指纹去重，避免把不同的短帖误判成同一篇
- 表按 TTL 淘汰过期项，并按 `MAX_SENT_FP = 2000` 条截断，不会无限膨胀

它能兜住这两类"看起来像 bug"的情况：

| 情况 | ID 层能否拦住 | 指纹层 |
|---|---|---|
| 同一条线报被不同人重发（ID 不同、正文一样） | ❌ 拦不住 | ✅ **拦住** |
| 消息其实已送达，但 HTTP 响应超时被误判为失败 → 重试 | ❌ 会重发 | ⚠️ 部分拦住（首次已记录指纹时有效） |

⚠️ 指纹表存在 `state` 里，**状态文件整个丢失时指纹也一起丢**，所以它兜不住"状态丢失"。
那条路靠两件事防：`seen` 保留 5000 条 + 每轮都提交状态（提交失败会打 warning）。

> 命中重复时日志是：`[WARN] 内容重复，跳过 #xxxx（同内容已于 N 小时前推送过）`
> 被跳过的帖子仍会被标记已读，**不会进 `pending` 反复重试**。

---

## 五、退出码（决定 run 红不红）

| 码 | 含义 | workflow 的行为 |
|---|---|---|
| `0` | 正常（含"无新帖"） | 继续循环 |
| `2` | 一个推送渠道都没配 | **立即终止 job**（配置错误，重试无意义） |
| `3` | 有帖子要推但一条都没成功 | 连续 **3 轮**如此即终止 job |

终止时 run 变红，GitHub 会发失败通知 —— 这是"故障可见"的唯一途径。

> ⚠️ 长循环里 `python3 … || RC=$?` **必须接住退出码**。以前写的是 `|| echo "::warning::"`，
> 退出码被吞掉，于是即使一条都推不出去，循环也会跑满 340 分钟且 run 依然绿色。

---

## 六、本地常用操作

```bash
# 跑一轮（Actions 用的就是这个）
python actions_monitor.py --once

# 空跑：只打印不推送（不会污染 state，因为不写 seen）
python actions_monitor.py --dry

# 各渠道连通性测试
python actions_monitor.py --test

# 补推最近 N 条
python actions_monitor.py --backfill 3

# 回归测试（打桩 + 本地假钉钉，不会发真实消息）
python test_hxm5.py

# 看线上调度实况
python heartbeat.py --status

# 手动催一次抓取
python heartbeat.py
```

---

## 七、凭据管理

**所有密钥都用 Windows DPAPI 加密存在 `~/.workbuddy/secrets/`，明文一律不落地。**

```bash
python ~/wbcrypt.py list                 # 看有哪些
python ~/wbcrypt.py get gh_token         # 取出来（会打印到终端）
echo <新值> | python ~/wbcrypt.py save <名字>
```

当前在库：`gh_token`、`dingtalk_webhook`、`dingtalk_secret`、`wecom_webhook`、
`tc_secret_id`、`tc_secret_key`。

> ⚠️ **历史遗留提醒**：`deploy_scf.py` 曾在**公开仓库**里硬编码过一个完整的企微 webhook。
> 2026-10-13 已改为从加密库读取。**那个旧 webhook 建议在企微后台删除对应机器人**——
> 虽然企微推送已关闭，但凭据本身曾公开暴露过。

---

## 八、技术要点（以后维护用）

站点是 JS 动态渲染的 SPA，静态 HTML 没有数据，走自研 JSON 接口并**带防爬签名**，
算法逆向自 `/ui/app.js`：

```
jt = floor(Date.now() / 180000)                          # 5 分钟一档的时间片
jx = FNV1a32("<path>|<querystring>|hxm5-json-guard-v1")  # 再转 36 进制
```

请求要求：`POST` + `Content-Type: application/x-www-form-urlencoded`，
且必须带 `X-HXM5-JSON: 1`、`X-Requested-With: XMLHttpRequest`。

- 列表：`/xianbao/{cid}/json/`，参数 `r=list`
- 详情：`/t/{id}/json/`，参数 `r=thread&id={id}`
- 统一返回 `{"code":200,"data":{...}}`

**若失效**：多半是站点改了盐值，重新拉 `/ui/app.js` 搜 `JSON_GUARD_SALT` 和 `jsonGuardHash`，
改 `actions_monitor.py` 顶部的 `SALT`。

### 钉钉的两个坑（已踩过，别回退）

1. **链接必须用代码块包住**：
   ```
   原帖：
   ```
   https://www.hxm5.com/t/xxxx
   ```
   ```
   裸 URL 会被钉钉自动链接化，然后**点击被安全网关拦 + 复制出来是 URL 编码**两个坑同时中招。
   代码块里的原文才能长按干净复制。

2. **尾部链接不参与截断**：构建消息时先给链接块预留字节额度，再截正文。
   否则长帖会把链接连同 ` ``` ` 闭合围栏一起切掉，链接方案静默失效。

---

## 九、已知限制

- **调度不准**：GitHub `schedule` 实测 2~7 小时才触发一次。长循环只在单次 job 内有效，
  job 之间的空档依然存在。要真正拉满需外部心跳（见 `HEARTBEAT_SETUP.md`）。
- **长循环占并发槽**：同一时刻只有一个 run，新的触发会 pending 排队。
- **钉钉限流 20 条/分钟**，超过会被禁言约 10 分钟。
- 仓库**必须保持 public**：改 private 会瞬间用穿免费额度的 2000 分钟/月。
- 群聊若开启"全员禁言"，推送会失败。
