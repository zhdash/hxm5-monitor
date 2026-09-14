# 云函数（SCF）部署说明 —— ⛔ 已于 2026-09-14 17:00 彻底关停

> **状态：已删除，不再产生任何费用。**
> 关停原因：**被扣费**（详见下方「八、扣费事件复盘」）。
> 代码与部署脚本全部保留，需要时可 `python deploy_scf.py` 一键重建。

---

## 〇、关停记录（2026-09-14 17:00）

| 资源 | 操作 | 结果 |
|---|---|---|
| 触发器 `every-minute` | `DeleteTrigger` | ✅ 已删除 |
| 函数 `hxm5-monitor` | `DeleteFunction` | ✅ 已删除（复查 `ListFunctions` = 0 个） |
| COS 对象 3 个 | 逐个 `DELETE` | ✅ 三个 HTTP 204 |
| COS 桶 `hxm5-state-1257013435` | `DELETE /` | ✅ HTTP 204 |

备份位置：`backup_before_delete/backup.json`（含函数配置、11 个环境变量、触发器定义）。

⚠️ **`credentials.json` 里的明文密钥建议去控制台轮换**。

---

## 一、关停前的部署清单（存档）

| 项目 | 值 |
|---|---|
| 函数名 | `hxm5-monitor` |
| 地域 | 广州 `ap-guangzhou` |
| 命名空间 | `default` |
| 运行环境 | Python 3.10 |
| 执行方法 | `scf_monitor.main_handler` |
| 超时 | 60 秒 |
| 内存 | 128 MB |
| 运行角色 | `SCF_QcsRole` |
| 定时触发器 | `every-minute`，Cron `0 * * * * * *`（每分钟） |
| 状态存储 | COS 桶 `hxm5-state-1257013435`，对象 `state/hxm5_state.json` |

部署包内容：`scf_monitor.py` + `cos_state.py`

---

## 二、环境变量（共 11 个）

| Key | Value |
|---|---|
| `HXM5_CID` | `3` |
| `HXM5_WECOM_WEBHOOK` | 企微「消息推送」Webhook |
| `HXM5_BARK_LEVEL` | `timeSensitive` |
| `HXM5_MAX_IMAGES` | `3` |
| `HXM5_REPLY_LIMIT` | `15` |
| `HXM5_FALLBACK_MIN` | `3` |
| `HXM5_COS_BUCKET` | `hxm5-state-1257013435` |
| `HXM5_COS_REGION` | `ap-guangzhou` |
| `HXM5_COS_KEY` | `state/hxm5_state.json` |
| `HXM5_COS_SECRET_ID` | API 密钥 SecretId |
| `HXM5_COS_SECRET_KEY` | API 密钥 SecretKey |

> 改环境变量不需要重新部署代码，下一分钟即生效。

---

## 三、一键部署脚本

```bash
cd deliverables/hxm5-monitor
python deploy_scf.py
```

脚本会自动：打包代码 → 创建/更新函数 → 写环境变量 → 建触发器 → 手动调用验证。

---

## 四、为什么必须用 COS 存状态

云函数是**无状态容器**，实例随时被回收，`/tmp` 不保证存活。

早期版本把去重状态存 `/tmp`，实测每次执行都是冷启动、`/tmp` 全空，于是每轮都退化为
「只处理最近 3 分钟的帖子」—— 结果是**每分钟把最近 3 分钟内的 33 条帖子重推一遍**，
手机持续收重复消息，且单轮耗时 39 秒接近超时。

改成 COS 持久化后：
- 每轮从 COS 读取已推送的帖子 ID
- 精确去重，**一条不重不漏**
- 「无新帖」时单轮耗时显著下降

---

## 五、部署过程中踩过的坑（重要，避免重犯）

| 现象 | 根因 | 解法 |
|---|---|---|
| `InternalError: 服务处理出错` | 没传 `Role` 参数 | 传 `Role` |
| `ResourceNotFound.Role` | 账号下没有 SCF 服务角色 | 新建 `SCF_QcsRole` |
| `InvalidParameter.ParamError: roleArn error` | `Role` 填了 ARN | **只填角色名** `SCF_QcsRole` |
| 函数 `CreateFailed`，报 `cls:DescribeLogsets` 无权限 | 角色没绑策略 | 绑 `QcloudAccessForScfRole`(28341895) + `QcloudCLSFullAccess`(534803) |
| `PolicyId` 类型错误 | 传了字符串 | 必须传 **uint64 数字** |
| COS `SignatureDoesNotMatch` | format_string 里 headers 用 `;` 连接 | 必须用 **`&`** 连接（详见 `cos_state.py`） |
| SecretKey 校验失败 | 截图 OCR 把大写 `C` 认成小写 `c` | 手打或复制粘贴 |

---

## 六、费用 —— ⚠️ 原文写的「永久免费」是错的

### ❌ 错误结论（原文）
> 调用次数 4.32 万/100 万 = 4.3%，资源 1.6 万/40 万 = 4% → "永久免费"

**错在哪**：算的是「每分钟调用一次」的理论值，**完全漏算了 SCF 的实例存活计费**。

### ✅ 真实计费模型
腾讯云 SCF 按 **`实例存活时长 × 配置内存`** 计费（单位 GBs），**不是按"函数执行了多少秒"**：

```
计费量(GBs) = 内存(GB) × 实例存活秒数
```

- 本函数内存 **128MB = 0.125GB**
- 实例被拉起后**不会立刻回收**，会挂住一段时间
- **0.125GB × 3600s ≈ 43.2 GBs**（账单实测值 `43.215375 GBs`，完全吻合）

### ✅ 账单实证（`billing:DescribeBillDetail`）
| 字段 | 值 |
|---|---|
| `BusinessCodeName` | 云函数SCF |
| `ResourceId` | `default/function/hxm5-monitor` |
| `ComponentCode` | `v_scf_gbs`（资源用量） |
| `UsedAmount` | **43.215375 GBs** |
| `SinglePrice` | 0.00011108 元/GBs/小时 |
| `RealCost` | **0.00480036 元** |

→ **每小时 0.0048 元 ≈ 0.115 元/天 ≈ 3.5 元/月**（单个常驻实例）

### ⚠️ 三条关键认知
1. **「停用触发器」≠「停止计费」**。触发器停了只是不再有新的调用，**已拉起的实例在被回收前继续按小时计费**。要彻底停只有**删掉函数**。
2. **免费额度是账户级共享的**（100 万次调用 + 40 万 GB·秒/月），**不是每个函数各给一份**。账号里其他服务会一起抢这个额度。
3. **内存调大 = 直接按比例放大成本**。128MB 是 0.125GB，若设成 512MB 就是 4 倍。

### 如果以后要重建，省钱的调法
- **内存调到最低 64MB**（成本直接减半）——本项目只是抓 HTTP + 拼 JSON，64MB 足够。
- 或把触发间隔从 1 分钟改成 **5 分钟**（成本降到 1/5）。
- 或改用**轻量服务器 crontab**（已有 `server_monitor.py`，服务器是包月固定价，跑多少都不额外收费）。

---

## 七、怎么停 / 怎么改

**临时停止（⚠️ 不省钱，实例仍计费）**：函数详情 → 触发器管理 → 停用 `every-minute`

**彻底停止计费**：删除函数 `hxm5-monitor`（可选同时删 COS 桶）

**换板块**：改环境变量 `HXM5_CID`（3=热门活动，0=最新线报）

**不再重复推送**：确认 `HXM5_COS_*` 四个变量都有值

**查看日志**：函数详情 → 日志查询

---

## 八、扣费事件复盘（2026-09-14）

### 时间线
1. 15:08 部署上线，触发器每分钟跑，实测正常。
2. 16:47 修完「双端重复推送」问题。
3. 17:00 前后用户反馈**被扣费** → 立即 `UpdateTrigger(Enable=CLOSE)` 止血。
4. 查账单定位根因：**SCF 资源用量计费**（非 COS，COS 存储费仅几 KB ≈ 0）。
5. 用户确认后**全部删除**：触发器 + 函数 + COS 对象 + COS 桶。复查 4 项全清。

### 账单 API 用法备忘（`billing.tencentcloudapi.com` / `2018-07-09`）
- `DescribeAccountBalance` —— **对普通账号返回全 None / -1**，没用，别查它。
- `DescribeBillDetail(Month, Offset, Limit, NeedRecordNum)` —— **权威源**。
  - ⚠️ **金额不在顶层**！`RealTotalCost` / `TotalCost` 等字段全是空，**必须在 `ComponentSet[]` 里取 `RealCost` / `CashPayAmount` / `UsedAmount` / `SinglePrice` / `PriceUnit`**。
  - 只按顶层字段看会看到"金额 0.0000"，从而**误判成没扣钱**（本次差点踩到）。
- `DescribeBillSummaryByProduct` 需额外传 `BeginTime`，只传 `Month` 报 `MissingParameter`。
- `BusinessCodeName = "月度计费精度差异"` 是正常噪音，忽略。

### 一句话教训
> **云函数不是"跑一次收一次钱"，是"实例醒着就按内存×时长收钱"。**
> 停触发器只是不给它新活儿，**要让它闭嘴必须把函数删了**。
