# legacy/ —— 已停用的历史实现

> 建立于 2026-10-13。**这里的东西都不再运行**，仅作存档。

## 为什么要分开

监控逻辑曾在 4 个平台上各复制一份，`server_monitor.py` 与 `actions_monitor.py`
的重复率高达 **80.1%**，20 个同名函数散落在 3~4 个文件里，且实现已经开始分叉。

这直接造成过一次线上事故：**2026-09-24 关闭企微后，脚本一条都不推**——
因为「没配任何渠道就退出」的守卫在 `actions_monitor.py` 里漏判了钉钉，
而两份代码是各写各的，改一处漏一处。

所以现在**只保留 `actions_monitor.py` 一份活的实现**，其余全部移到这里。
改动线上行为时请只改仓库根目录的那一份。

## 归档清单

| 文件 | 停用原因 |
|---|---|
| `hxm5_monitor.py` | 最早的本地常驻版，已被 actions 版取代 |
| `scf_monitor.py` | 腾讯云 SCF 版，云函数已于 2026-09-22 销毁 |
| `server_monitor.py` | 腾讯云轻量服务器 crontab 版，实例已于 2026-09-22 销毁 |
| `cos_state.py` | COS 状态持久化，随 SCF 一起废弃 |
| `deploy_scf.py` | SCF 部署脚本，函数已销毁 |
| `tc3.py` | 腾讯云 API 签名库，仅被上面的 SCF 脚本使用 |
| `check_status.py` | 旧的多平台状态检查 |
| `start_monitor.py` | 启动 hxm5_monitor.py 的辅助脚本 |
| `preview.py` | 本地预览辅助 |
| `1-start.bat` | 旧本地版启动 |
| `2-stop.bat` | 旧本地版停止 |
| `SCF_DEPLOY.md` | SCF 部署文档，函数已销毁 |

## 相关事实

- 腾讯云账户（Uin 100006289397）已于 **2026-09-22 15:45 彻底销毁**，余额 0.00；
  轻量服务器 / CVM / SCF / COS / CLS 等资源全部为 0。
- 这些脚本引用的凭据已转入 DPAPI 加密库 `~/.workbuddy/secrets/`
  （`python ~/wbcrypt.py list` 查看），源码与配置里不再有明文。
- 当前线上只有一个入口：仓库根的 `.github/workflows/monitor.yml` + `actions_monitor.py`。
