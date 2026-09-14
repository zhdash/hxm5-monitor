# -*- coding: utf-8 -*-
"""
一键部署 hxm5-monitor 到腾讯云云函数 SCF（广州）
纯标准库，不依赖腾讯云 SDK
"""
import os
import sys
import io
import json
import time
import base64
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tc3 import TC3

HERE = os.path.dirname(os.path.abspath(__file__))

# 凭据从环境变量读取（不硬编码）
# 运行前设置：
#   set TC_SECRET_ID=AKIDxxxx
#   set TC_SECRET_KEY=xxxx
# 或写入 credentials.json（已 gitignore）
SID = os.environ.get("TC_SECRET_ID", "")
SKEY = os.environ.get("TC_SECRET_KEY", "")
_cred = os.path.join(HERE, "credentials.json")
if (not SID or not SKEY) and os.path.exists(_cred):
    try:
        with open(_cred, encoding="utf-8") as _f:
            _c = json.load(_f)
        SID = SID or _c.get("secret_id", "")
        SKEY = SKEY or _c.get("secret_key", "")
    except Exception:
        pass

if not SID or not SKEY:
    print("缺少腾讯云凭据。请设置环境变量 TC_SECRET_ID / TC_SECRET_KEY，")
    print("或在 %s 写入 {\"secret_id\":\"...\", \"secret_key\":\"...\"}" % _cred)
    sys.exit(1)
REGION = "ap-guangzhou"
FUNC_NAME = "hxm5-monitor"
NAMESPACE = "default"
RUNTIME = "Python3.10"
HANDLER = "scf_monitor.main_handler"
TIMEOUT = 60
MEMORY = 128

WECOM = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=3a99c1e4-47ad-4f7a-961d-ea228656743f"
BARK_KEY = ""   # 由用户在后续配置；如需注入请填这里

# COS 状态存储（解决云函数无状态导致的重复推送）
COS_BUCKET = "hxm5-state-1257013435"
COS_REGION = "ap-guangzhou"
COS_KEY = "state/hxm5_state.json"

# 云函数运行角色：只需填角色名，不是 ARN（填 ARN 会报 roleArn error）
ROLE_NAME = "SCF_QcsRole"

tc = TC3(SID, SKEY, REGION)


def log(m):
    print(m)
    sys.stdout.flush()


def build_zip():
    """把 scf_monitor.py + cos_state.py 打成 zip（根目录直接放文件，符合 SCF 要求）"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for fn in ["scf_monitor.py", "cos_state.py"]:
            src = os.path.join(HERE, fn)
            with open(src, "rb") as f:
                data = f.read()
            z.writestr(fn, data)
            log("  打包 %s (%d 字节)" % (fn, len(data)))
    raw = buf.getvalue()
    log("  ZIP 大小 %d 字节" % len(raw))
    return base64.b64encode(raw).decode()


def env_vars():
    v = [
        {"Key": "HXM5_CID", "Value": "3"},
        {"Key": "HXM5_WECOM_WEBHOOK", "Value": WECOM},
        {"Key": "HXM5_BARK_LEVEL", "Value": "timeSensitive"},
        {"Key": "HXM5_MAX_IMAGES", "Value": "3"},
        {"Key": "HXM5_REPLY_LIMIT", "Value": "15"},
        {"Key": "HXM5_FALLBACK_MIN", "Value": "3"},
        {"Key": "HXM5_COS_BUCKET", "Value": COS_BUCKET},
        {"Key": "HXM5_COS_REGION", "Value": COS_REGION},
        {"Key": "HXM5_COS_KEY", "Value": COS_KEY},
        {"Key": "HXM5_COS_SECRET_ID", "Value": SID},
        {"Key": "HXM5_COS_SECRET_KEY", "Value": SKEY},
    ]
    if BARK_KEY:
        v.append({"Key": "HXM5_BARK_KEY", "Value": BARK_KEY})
    return {"Variables": v}


def main():
    log("=== 1. 检查函数是否已存在 ===")
    try:
        r = tc.call("ListFunctions", {"Limit": 100})
        existing = [f.get("FunctionName") for f in (r.get("Functions") or [])]
        log("  现有函数：%s" % (existing or "(无)"))
    except Exception as e:
        log("  列表失败：%s" % e)
        existing = []

    log("")
    log("=== 2. 打包代码 ===")
    code_b64 = build_zip()

    env = env_vars()

    if FUNC_NAME in existing:
        log("")
        log("=== 3a. 函数已存在，更新代码+配置 ===")
        tc.call("UpdateFunctionCode", {
            "FunctionName": FUNC_NAME,
            "Namespace": NAMESPACE,
            "Handler": HANDLER,
            "ZipFile": code_b64,
            "Publish": "FALSE",
        })
        log("  代码已更新")
        tc.call("UpdateFunctionConfiguration", {
            "FunctionName": FUNC_NAME,
            "Namespace": NAMESPACE,
            "Timeout": TIMEOUT,
            "MemorySize": MEMORY,
            "Environment": env,
            "Description": "线报屋 hxm5.com 新帖监控 -> 企微/Bark 推送",
        })
        log("  配置已更新（超时 %ds / 内存 %dMB / 环境变量）" % (TIMEOUT, MEMORY))
    else:
        log("")
        log("=== 3b. 新建函数 ===")
        params = {
            "FunctionName": FUNC_NAME,
            "Namespace": NAMESPACE,
            "Runtime": RUNTIME,
            "Handler": HANDLER,
            "Timeout": TIMEOUT,
            "MemorySize": MEMORY,
            "Description": "线报屋 hxm5.com 新帖监控 -> 企微/Bark 推送",
            "Code": {"ZipFile": code_b64},
            "Environment": env,
            "Role": ROLE_NAME,
        }
        r = tc.call("CreateFunction", params)
        log("  创建成功：%s" % r.get("FunctionName") or FUNC_NAME)
        log("  状态：%s" % r.get("Status"))
        log("  Role：%s" % r.get("Role"))

    log("")
    log("=== 4. 等待函数就绪 ===")
    for i in range(20):
        try:
            r = tc.call("GetFunction", {"FunctionName": FUNC_NAME, "Namespace": NAMESPACE})
            st = r.get("Status")
            log("  [%02d] Status=%s  Timeout=%s  Memory=%s  Runtime=%s"
                % (i, st, r.get("Timeout"), r.get("MemorySize"), r.get("Runtime")))
            if st in ("Active", "active"):
                break
        except Exception as e:
            log("  查询异常：%s" % e)
        time.sleep(3)

    log("")
    log("=== 5. 配置环境变量（确保生效）===")
    try:
        tc.call("UpdateFunctionConfiguration", {
            "FunctionName": FUNC_NAME,
            "Namespace": NAMESPACE,
            "Timeout": TIMEOUT,
            "MemorySize": MEMORY,
            "Environment": env,
        })
        log("  已写入 %d 个环境变量" % len(env["Variables"]))
        for v in env["Variables"]:
            shown = v["Value"]
            if v["Key"] in ("HXM5_WECOM_WEBHOOK", "HXM5_BARK_KEY") and len(shown) > 30:
                shown = shown[:30] + "..."
            log("    %s = %s" % (v["Key"], shown))
    except Exception as e:
        log("  环境变量写入失败：%s" % e)

    log("")
    log("=== 6. 创建定时触发器 ===")
    try:
        r = tc.call("ListTriggers", {"FunctionName": FUNC_NAME, "Namespace": NAMESPACE})
        trigs = r.get("Triggers") or []
        log("  现有触发器：%s" % ([t.get("TriggerName") for t in trigs] or "(无)"))
        if any(t.get("TriggerName") == "every-minute" for t in trigs):
            log("  触发器 every-minute 已存在，跳过")
        else:
            tc.call("CreateTrigger", {
                "FunctionName": FUNC_NAME,
                "Namespace": NAMESPACE,
                "TriggerName": "every-minute",
                "Type": "timer",
                "TriggerDesc": "0 * * * * * *",
                "Enable": "OPEN",
            })
            log("  触发器 every-minute 已创建（Cron: 0 * * * * * *）")
    except Exception as e:
        log("  触发器创建失败：%s" % e)

    log("")
    log("=== 7. 手动调用验证 ===")
    try:
        r = tc.call("Invoke", {
            "FunctionName": FUNC_NAME,
            "Namespace": NAMESPACE,
        }, retries=1)
        log("  Result: %s" % r.get("Result"))
        log("  FunctionError: %s" % (r.get("FunctionError") or "(无)"))
        log("  Duration: %sms" % r.get("Duration"))
    except Exception as e:
        log("  调用失败（不影响定时触发）：%s" % e)

    log("")
    log("=== 部署完成 ===")


if __name__ == "__main__":
    main()
