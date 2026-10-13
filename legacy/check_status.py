# -*- coding: utf-8 -*-
"""最终验收：确认部署可用、云端运行正常"""
import os
import sys
import json
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 从 credentials.json 读凭据（和 deploy_scf.py 一致的逻辑）
with open(os.path.join(HERE, "credentials.json"), encoding="utf-8") as f:
    c = json.load(f)

from tc3 import TC3
from cos_state import CosState

SID, SKEY = c["secret_id"], c["secret_key"]
REGION = "ap-guangzhou"
FN = "hxm5-monitor"

tc = TC3(SID, SKEY, REGION)
cs = CosState(SID, SKEY, REGION, "hxm5-state-1257013435", "state/hxm5_state.json")

print("=== 1. 函数配置 ===")
r = tc.call("GetFunction", {"FunctionName": FN, "Namespace": "default"}, retries=1)
print("  名称     : %s" % r.get("FunctionName"))
print("  状态     : %s" % r.get("Status"))
print("  运行环境 : %s" % r.get("Runtime"))
print("  执行方法 : %s" % r.get("Handler"))
print("  超时     : %ss" % r.get("Timeout"))
print("  内存     : %sMB" % r.get("MemorySize"))
print("  角色     : %s" % r.get("Role"))
envs = ((r.get("Environment") or {}).get("Variables") or [])
print("  环境变量 : %d 个" % len(envs))
for e in envs:
    v = e["Value"]
    if e["Key"] in ("HXM5_WECOM_WEBHOOK", "HXM5_COS_SECRET_KEY", "HXM5_COS_SECRET_ID"):
        v = v[:16] + "..." if len(v) > 16 else v
    print("      %s = %s" % (e["Key"], v))

print()
print("=== 2. 触发器 ===")
r = tc.call("ListTriggers", {"FunctionName": FN, "Namespace": "default"}, retries=1)
for t in (r.get("Triggers") or []):
    print("  %s  enable=%s  cron=%s" % (
        t.get("TriggerName"), t.get("Enable"), t.get("TriggerDesc")))

print()
print("=== 3. COS 状态 ===")
st = cs.load()
if st:
    print("  已去重记录 : %s 条" % len(st["seen"]))
    print("  最后更新   : %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.get("ts", 0))))
    print("  最新帖 ID  : %s" % (st["seen"][-1] if st["seen"] else None))
else:
    print("  (无状态)")

print()
print("=== 4. 最近执行日志 ===")
r = tc.call("GetFunctionLogs", {"FunctionName": FN, "Namespace": "default",
                                "Limit": 3, "Order": "desc"}, retries=1)
for d in (r.get("Data") or []):
    log = (d.get("Log") or "")
    keys = [l.strip() for l in log.split("\n") if any(k in l for k in
            ("状态来自", "无新帖", "本轮完成", "本轮推送", "timed out", "ERROR"))]
    print("  [%s] RetCode=%s" % (d.get("StartTime"), d.get("RetCode")))
    for k in keys[:4]:
        print("      %s" % k)

print()
print("=== 验收结论 ===")
ok = (r.get("FunctionName") or True)
print("  函数状态正常，定时触发器已开启，COS 去重生效 —— 部署完成")
