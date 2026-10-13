# -*- coding: utf-8 -*-
"""hxm5 监控回归测试套件

跑法：  python test_hxm5.py

覆盖：
  A. 载荷构建 —— 长正文不得丢掉尾部原帖链接 / 代码块围栏；载荷不得超上限
  B. 去重重试状态机 —— 推送失败不得标记已读、达上限才放弃、旧状态文件兼容
  C. 端到端 —— 起本地假钉钉服务，真实抓取 + 真实 HTTP POST（不骚扰真实的钉钉群）
  D. 杂项 —— 归档开关、状态损坏告警、退出码语义

设计原则：**打桩替代真实推送**，绝不向真实渠道发消息。
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "actions_monitor.py")
PY = sys.executable

PASS, FAIL = [], []


def chk(cond, label):
    (PASS if cond else FAIL).append(label)
    print("   %s %s" % ("✅" if cond else "❌", label))


def load_module():
    spec = importlib.util.spec_from_file_location("am", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["am"] = mod
    spec.loader.exec_module(mod)
    return mod


am = load_module()
am.load_config()


def make_post(tid="7777777", content_chars=0, replies=0, reply_len=140):
    return {"ID": tid, "title": "标题", "Content": "中" * content_chars,
            "nick": "某人", "time": "2026-10-13 12:00",
            "reply": [{"nick": "r%d" % i, "Content": "回" * reply_len}
                      for i in range(replies)],
            "img": []}


# ============================================================ A. 载荷构建
print("=" * 74)
print("A. 载荷构建：链接与围栏必须永远完整")
print("=" * 74)
for label, cc, rp in [("短帖", 50, 0), ("中帖", 500, 5), ("长帖", 1600, 0),
                      ("超长帖", 30000, 0), ("回复爆炸", 0, 60), ("超长+多回复", 5000, 40)]:
    t = make_post(content_chars=cc, replies=rp)
    _, dt = am.build_dingtalk(t, "热门活动")
    md = am.build_markdown(t, "热门活动")
    chk(dt.rstrip().endswith("```") and "/t/7777777" in dt,
        "钉钉 %s：末尾代码块围栏完整且含原帖链接" % label)
    chk(len(dt.encode()) <= am.DT_LIMIT,
        "钉钉 %s：载荷 %d <= %d" % (label, len(dt.encode()), am.DT_LIMIT))
    chk("[查看原帖]" in md, "企微 %s：含查看原帖链接" % label)
    chk(len(md.encode()) <= am.WECOM_LIMIT,
        "企微 %s：载荷 %d <= %d" % (label, len(md.encode()), am.WECOM_LIMIT))

_tn = [("ascii", "a" * 100, 10), ("中文", "中" * 100, 10), ("正常额度", "中" * 5000, 4000),
       ("额度小于标记", "a" * 100, 5), ("额度等于标记", "a" * 100, len(am.TRUNC_MARK.encode()))]
for _lbl, _s, _lim in _tn:
    _r = am.truncate_bytes(_s, _lim)
    chk(len(_r.encode()) <= _lim,
        "truncate_bytes[%s]：结果 %d 字节 <= 上限 %d" % (_lbl, len(_r.encode()), _lim))
    chk("�" not in _r, "truncate_bytes[%s]：没有产生乱码" % _lbl)

# ============================================================ B. 状态机
print()
print("=" * 74)
print("B. 去重重试状态机")
print("=" * 74)

tmpdir = tempfile.mkdtemp(prefix="hxm5_test_")
STATE = os.path.join(tmpdir, "state.json")
am.STATE_PATH = STATE

POSTS = {"1001": "帖一", "1002": "帖二", "1003": "帖三"}
BASE = ["0901", "0902"]
OK_SET = set()
CALLS = []

am.fetch_list = lambda cid, page=1: {
    "title": "热门活动",
    "list": [{"ID": k, "Title": v} for k, v in POSTS.items()]
            + [{"ID": b, "Title": "老"} for b in BASE],
}
am.fetch_thread = lambda tid: {"ID": str(tid), "title": POSTS.get(str(tid), ""),
                               "Content": "正文", "nick": "n", "time": "t",
                               "reply": [], "img": []}


def fake_push(t, cid_title):
    CALLS.append(str(t["ID"]))
    return str(t["ID"]) in OK_SET


am.push_all = fake_push
am.archive_post = lambda t: None
os.environ["HXM5_DINGTALK_WEBHOOK"] = "http://stub.invalid/x"
am.load_config()


def st():
    return json.load(open(STATE, encoding="utf-8"))


json.dump({"seen": list(BASE), "initialized": True}, open(STATE, "w", encoding="utf-8"))

print("  -- 第 1 轮：3 条新帖全部失败")
CALLS.clear(); OK_SET.clear()
rc = am.run_once()
s = st()
chk(rc == am.EXIT_ALL_PUSH_FAILED, "退出码 3（全部失败）")
chk(all(t not in s["seen"] for t in POSTS), "失败的帖子没有被标记已读")
chk(set(s["pending"]) == set(POSTS), "3 条进 pending 待补发")
chk(all(v == 1 for v in s["pending"].values()), "失败计数 = 1")

print("  -- 第 2 轮：仍全失败，且不得重复推送")
CALLS.clear()
rc = am.run_once()
s = st()
chk(len(CALLS) == 3, "只补发 3 次（pending 不被当新帖重复推）")
chk(all(v == 2 for v in s["pending"].values()), "失败计数累加到 2")
chk(rc == am.EXIT_ALL_PUSH_FAILED, "补发全失败也返回 3")

print("  -- 第 3 轮：1001 成功、其余达上限放弃")
OK_SET = {"1001"}
rc = am.run_once()
s = st()
chk("1001" in s["seen"] and "1001" not in s["pending"], "成功的移入 seen")
chk("1002" in s["seen"] and "1003" in s["seen"], "连续失败 3 次的放弃并标记")
chk(not s["pending"], "pending 清空，不会积压堵队列")

print("  -- 第 4 轮：无新帖")
chk(am.run_once() == am.EXIT_OK, "退出码 0")

print("  -- 旧格式状态文件（无 pending 字段）")
json.dump({"seen": list(BASE) + list(POSTS), "initialized": True},
          open(STATE, "w", encoding="utf-8"))
chk(am.run_once() == am.EXIT_OK, "旧格式可正常读取")

print("  -- 无渠道")
os.environ.pop("HXM5_DINGTALK_WEBHOOK", None)
am.load_config()
chk(am.run_once() == am.EXIT_NO_CHANNEL, "退出码 2")
os.environ["HXM5_DINGTALK_WEBHOOK"] = "http://stub.invalid/x"
am.load_config()

print("  -- 状态文件损坏")
open(STATE, "w").write("{ 这不是合法 json")
chk(am.run_once() == am.EXIT_OK, "损坏时安全退回（重建基线，不推送）")

# ============================================================ D. 杂项
print()
print("=" * 74)
print("D. 杂项")
print("=" * 74)
chk(am.DEFAULT_PUSH_IMAGES is False, "push_images 默认值单一出处 = False")
src = open(SCRIPT, encoding="utf-8").read()
chk('CONFIG.get("push_images", True)' not in src, "调用点不再硬写矛盾的默认值 True")
chk("ARCHIVE_ENABLED" in src, "归档有开关")
chk(am.ARCHIVE_ENABLED is False, "归档默认关闭")

# ============================================================ C. 端到端
print()
print("=" * 74)
print("C. 端到端（真实抓取 + 真实 HTTP，打到本地假钉钉）")
print("=" * 74)

MODE = {"reply": {"errcode": 0, "errmsg": "ok"}}
HITS = []


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        HITS.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        out = json.dumps(MODE["reply"]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
E2E = os.path.join(tmpdir, "e2e.json")
URL = "http://127.0.0.1:%d/robot?access_token=stub" % srv.server_address[1]


def run_real():
    env = dict(os.environ)
    env["HXM5_DINGTALK_WEBHOOK"] = URL
    env["HXM5_STATE_PATH"] = E2E
    env["HXM5_MAX_POSTS_PER_CYCLE"] = "3"
    env.pop("HXM5_WECOM_WEBHOOK", None)
    env.pop("HXM5_BARK_KEY", None)
    env.pop("HXM5_DINGTALK_SECRET", None)
    env.pop("HXM5_ARCHIVE", None)
    r = subprocess.run([PY, SCRIPT, "--once"], env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=300)
    return r.returncode, r.stdout


rc, out = run_real()
print("   基线轮 rc=%d，列表 %d 条" % (rc, len(json.load(open(E2E, encoding="utf-8"))["seen"])))
chk(rc == 0, "基线轮退出码 0")

s = json.load(open(E2E, encoding="utf-8"))
removed = s["seen"][:3]
s["seen"] = s["seen"][3:]
json.dump(s, open(E2E, "w", encoding="utf-8"), ensure_ascii=False)

HITS.clear()
rc, out = run_real()
s = json.load(open(E2E, encoding="utf-8"))
chk(rc == 0 and len(HITS) == 3, "成功路径：真实发出 3 次 POST，退出码 0")
chk(all(b"markdown" in h for h in HITS), "POST 体是钉钉 markdown 格式")
chk(all(t in s["seen"] for t in removed), "3 条都标记已读")
chk(not s["pending"], "pending 为空")

MODE["reply"] = {"errcode": 300001, "errmsg": "stub rejected"}
s = json.load(open(E2E, encoding="utf-8"))
removed2 = s["seen"][:3]                 # ⚠️ 必须重新取：上一轮的 removed 已经成功送出
s["seen"] = s["seen"][3:]
json.dump(s, open(E2E, "w", encoding="utf-8"), ensure_ascii=False)
rc, out = run_real()
s = json.load(open(E2E, encoding="utf-8"))
chk(rc == 3, "失败路径：退出码 3")
chk(all(t not in s["seen"] for t in removed2), "失败的没有被标记已读")
chk(set(s["pending"]) == set(removed2), "3 条进 pending")

MODE["reply"] = {"errcode": 0, "errmsg": "ok"}
rc, out = run_real()
s = json.load(open(E2E, encoding="utf-8"))
chk(rc == 0 and not s["pending"], "恢复后补发成功，pending 清空")

srv.shutdown()

# ============================================================ 汇总
print()
print("=" * 74)
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("   ❌", f)
    sys.exit(1)
print("✅ 全部通过")
