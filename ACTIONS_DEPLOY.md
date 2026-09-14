# 线报屋监控 · GitHub Actions 方案（永久免费）

> **为什么换过来**：腾讯云 SCF 按「配置内存 × 函数运行时长」计费。
> 我当初按每次 0.5 秒估算，实际每次要 3~5 秒，**低估了约 20 倍**，加上 CLS 日志费，
> 于是产生了实际扣费。已彻底删除 SCF 全部资源，改用 GitHub Actions。

---

## 一、为什么 GitHub Actions 是真的免费

| 对比项 | 腾讯云 SCF | GitHub Actions |
|---|---|---|
| 计费口径 | 内存 × 函数运行时长（GBs） | 按 **运行分钟数** |
| 免费额度 | 40 万 GBs/月（**账户级共享**） | 公开仓库 **无限**；私有仓库 2000 分钟/月 |
| 每次 5 秒的成本 | 0.125GB × 5s = 0.625 GBs | 按分钟进位，但公开仓库不计数 |
| 我们的月消耗 | 约 3 万 GBs（超预期 20 倍） | 约 1440 次 × 1 分钟 = **1440 分钟**（公开仓库 = 0 元） |
| 意外扣费风险 | **有**（日志/流量/额度共享） | 公开仓库 **无**，不绑卡就不会扣 |
| 最小间隔 | 1 分钟 | **5 分钟** |

**结论：公开仓库 = 无限分钟 = 永久 0 元，且不可能产生账单。**

---

## 二、架构（无状态，每轮全新虚拟机）

```
GitHub 定时触发（每 5 分钟，UTC）
        ↓
   全新 Ubuntu 虚拟机
        ↓
   ① actions/checkout  拉代码
        ↓
   ② 读 state/hxm5_state.json  ← 上一轮提交回来的去重状态
        ↓
   ③ 抓 hxm5.com 列表 → 比对 → 新帖抓详情 → 推企业微信
        ↓
   ④ git commit + push 把新状态写回仓库  ← 关键：这样下一轮才有记忆
```

⚠️ **和 SCF 的本质区别**：SCF 靠 COS 存状态（要钱）；Actions 直接**把状态 commit 进仓库**（免费且带完整历史）。

---

## 三、文件清单

| 文件 | 作用 |
|---|---|
| `actions_monitor.py` | 主程序（配置全走环境变量） |
| `.github/workflows/monitor.yml` | GitHub 定时任务定义 |
| `deploy_actions.py` | 一键部署脚本（建仓库、写密钥、写变量） |
| `gh_token.txt` | 放你的 GitHub token（**不要提交**） |

---

## 四、部署步骤

### 方式 A：全自动（推荐）

1. **生成 GitHub Token**
   访问 https://github.com/settings/tokens

   - **Fine-grained token**（推荐）：勾选
     - Contents → Read and write
     - Actions → Read and write
     - Secrets → Read and write
     - Administration → Read and write
   - 或 **Classic token**：勾 `repo` + `workflow`

2. **把 token 给我**，或者自己放进 `gh_token.txt`

3. **运行**
   ```bash
   cd deliverables/hxm5-monitor
   python deploy_actions.py
   ```

### 方式 B：手动

```bash
cd deliverables/hxm5-monitor
git init -b main
git add -A
git commit -m "feat: 线报屋监控 (GitHub Actions 版)"
git remote add origin https://github.com/<你的用户名>/hxm5-monitor.git
git push -u origin main
```

推完去仓库 **Settings → Secrets and variables → Actions**：

**Secret**（加密，必须）

| 名称 | 值 |
|---|---|
| `HXM5_WECOM_WEBHOOK` | 企微「消息推送」webhook |

**Variables**（明文，可改）

| 名称 | 推荐值 | 说明 |
|---|---|---|
| `HXM5_CID` | `3` | 3=热门活动，0=最新线报 |
| `HXM5_REPLY_LIMIT` | `15` | 每条帖子最多带几条回复 |
| `HXM5_PUSH_IMAGES` | `false` | 是否额外逐张推送图片（**会明显拖慢单轮**，建议 false） |
| `HXM5_MAX_IMAGES` | `3` | 开图片推送时最多几张 |
| `HXM5_MAX_POSTS_PER_CYCLE` | `12` | 单轮最多推几条 |
| `HXM5_BARK_LEVEL` | `timeSensitive` | Bark 通知级别（未配 Bark 则无效） |

### 环境变量速查（本地调试用）

```bash
HXM5_WECOM_WEBHOOK="https://qyapi.weixin.qq.com/..." \
HXM5_STATE_PATH=state/hxm5_state.json \
python3 actions_monitor.py --once     # 跑一轮
python3 actions_monitor.py --dry      # 只看不推
python3 actions_monitor.py --test     # 发测试消息
python3 actions_monitor.py --backfill 3   # 回溯推最近 3 条
```

---

## 五、注意事项

### ⚠️ 公开仓库的两个坑

1. **60 天不活动会被自动停用定时任务**
   GitHub 规定：公开仓库的 scheduled workflow **连续 60 天无仓库活动就自动禁用**。
   - 我们的 workflow 每 5 分钟会 commit 状态 → **本身就在产生活动**，所以不会触发。
   - 万一被停：去 Actions 页点「Enable workflow」，或 push 一次。

2. **不要往仓库里放密钥**
   代码里不存任何密钥，全部走 Secrets。公开仓库里只有代码和状态文件（帖子 ID 列表）。

### ⚠️ 5 分钟间隔的现实

- GitHub 的 cron 用 **UTC 时间**，表达式 `*/5 * * * *` 表示 UTC 每 5 分钟 —— 对北京时间同样成立（整点对齐）。
- **高峰期会延迟**，可能晚 1~10 分钟。GitHub 官方明确说明"不保证准时"。
- 对线报场景：晚几分钟通常不影响，热门活动不会秒没。

### ⚠️ 单轮的耗时构成（这也是换平台的原因）

| 步骤 | 耗时 |
|---|---|
| 检出代码 + 装 Python | ~15 秒（固定开销） |
| 抓列表 | ~1 秒 |
| 每抓一条详情 | ~0.5 秒 |
| 每条推送 | ~1~3 秒 |
| 每张图片（若开） | ~1~2 秒 |

**建议 `HXM5_PUSH_IMAGES=false`**，图片会显著拉长单轮时间和配额消耗。

---

## 六、费用

| 项目 | 用量 | 费用 |
|---|---|---|
| Actions 分钟数（公开仓库） | 无限 | **0 元** |
| 存储 | 几十 KB | **0 元** |
| 出站流量 | 免费 | **0 元** |

**不绑信用卡。不会有任何账单。**

---

## 七、怎么停 / 怎么改

**临时停**：仓库 → Actions → 选「线报屋监控」→ 右上 `···` → Disable workflow

**改频率**：编辑 `.github/workflows/monitor.yml` 里的 `cron`（最小 `*/5`）

**换板块**：仓库 → Settings → Variables → 改 `HXM5_CID`

**彻底删**：仓库 Settings → 最下方 Delete this repository

---

## 八、和之前方案的对比（为什么最终选这个）

| 方案 | 免费？ | 1 分钟？ | 风险 |
|---|---|---|---|
| 本机脚本 | ✅ | ✅ | 关机就停 |
| 轻量服务器 | ❌ 9/22 到期 | ✅ | 要续费 |
| 腾讯云 SCF | ❌ **已实测扣费** | ✅ | 计费口径易误判 |
| 华为云 FunctionGraph | ⚠️ 同 SCF 口径 | ✅ | 同样有误判风险 |
| **GitHub Actions** ✅ | ✅ **真免费** | ❌ 5 分钟 | 延迟不可控 |
| Oracle Always Free | ✅ | ✅ | 2026-08 起砍半到 2核12G，需信用卡验证 |

**选了 GitHub Actions**：公开仓库永不产生账单，这是最重要的一条。
如果哪天必须回到 1 分钟，再考虑 Oracle 的 ARM 永久免费实例。
