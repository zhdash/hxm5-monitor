# -*- coding: utf-8 -*-
"""
线报屋 (hxm5.com) 新帖监控 → 企业微信机器人推送

接口签名逆向自站点前端 app.js：
  jt = floor(now_ms / 180000)                      # 5 分钟时间片
  jx = FNV1a32(`${path}|${querystring}|hxm5-json-guard-v1`).toString(36)

用法：
  python hxm5_monitor.py              常驻轮询
  python hxm5_monitor.py --once       只跑一轮（不推送首屏历史帖）
  python hxm5_monitor.py --test       发一条测试消息到企业微信
  python hxm5_monitor.py --backfill 3 把最近 3 条帖子推一遍（验证效果）
"""
import os
import sys
import json
import time
import re
import html as htmllib
import logging
import urllib.request
import urllib.error
from logging.handlers import RotatingFileHandler
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
ARCHIVE_DIR = os.path.join(HERE, "archive")
LOG_PATH = os.path.join(HERE, "monitor.log")
PID_PATH = os.path.join(HERE, "monitor.pid")

BASE = "https://www.hxm5.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
SALT = "hxm5-json-guard-v1"
MASK = 0xFFFFFFFF
DIGS = "0123456789abcdefghijklmnopqrstuvwxyz"

DEFAULT_CONFIG = {
    "cid": 3,
    "interval_sec": 60,
    "wecom_webhook": "",
    "max_posts_per_cycle": 12,
    "include_replies": True,
    "include_images": True,
    "push_on_first_run": False,
    "reply_limit": 15,
}


# ---------------------------------------------------------------- 日志
def setup_logger():
    logger = logging.getLogger("hxm5")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


log = setup_logger()


# ---------------------------------------------------------------- 签名 / 请求
def _b36(n):
    if n == 0:
        return "0"
    s = ""
    while n:
        s = DIGS[n % 36] + s
        n //= 36
    return s


def _fnv1a(s):
    """对应 JS jsonGuardHash"""
    t = 2166136261
    for ch in s:
        t ^= ord(ch)
        t = (t * 16777619) & MASK
    return _b36(t)


def _signed_body(path, params):
    p = dict(params)
    p["jt"] = int(time.time() * 1000 // 180000)
    qs = urllib.parse.urlencode(p, quote_via=urllib.parse.quote)
    qs = qs + "&jx=" + _fnv1a("%s|%s|%s" % (path, qs, SALT))
    return qs


def api(path, params, retries=3):
    """POST 到站点的 JSON 接口，返回 data 部分"""
    body = _signed_body(path, params)
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(BASE + path, data=body.encode("utf-8"), headers={
                "User-Agent": UA,
                "Content-Type": "application/x-www-form-urlencoded",
                "X-HXM5-JSON": "1",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": BASE + path.split("/json/")[0] + "/",
            })
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            if data.get("code") != 200:
                raise RuntimeError("接口返回 %s: %s" % (data.get("code"), data.get("msg")))
            return data.get("data")
        except Exception as e:  # noqa
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError("请求 %s 失败: %s" % (path, last_err))


def fetch_list(cid, page=1):
    if page and page > 1:
        path = "/xianbao/%s/%s/json/" % (cid, page)
    else:
        path = "/xianbao/%s/json/" % cid
    p = {"r": "list"}
    if str(cid) == "3":
        p["type"] = "hot"
    elif str(cid) == "0":
        p["type"] = "index"
    else:
        p["cid"] = cid
    return api(path, p)


def fetch_thread(tid):
    return api("/t/%s/json/" % tid, {"r": "thread", "id": tid})


# ---------------------------------------------------------------- 文本清理
TAG_RE = re.compile(r"<[^>]+>")


def clean_text(s):
    if not s:
        return ""
    s = str(s)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r'<div class="quote">(.*?)</div>', r"\n[引用] \1\n", s, flags=re.S | re.I)
    s = re.sub(r"</(p|div|li|tr)>", "\n", s, flags=re.I)
    s = TAG_RE.sub("", s)
    s = htmllib.unescape(htmllib.unescape(s))
    s = re.sub(r"[ \t\u3000]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def truncate_bytes(s, limit):
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    return b[:limit].decode("utf-8", "ignore") + "\n\n…(内容过长已截断)"


# ---------------------------------------------------------------- 推送
WECOM_LIMIT = 4000  # 企微 markdown 上限 4096 字节，留余量


def build_wecom_markdown(t, cid_title):
    title = clean_text(t.get("title") or t.get("word") or "(无标题)")
    content = clean_text(t.get("Content"))
    nick = t.get("nick") or ""
    ttime = t.get("time") or ""
    tid = t.get("ID")
    replies = t.get("reply") or []
    imgs = t.get("img") or []

    lines = []
    lines.append("**线报屋 · %s**  <font color=\"comment\">%s</font>" % (cid_title, ttime))
    lines.append("")
    lines.append("### %s" % title)
    if content:
        lines.append("")
        lines.append(content)
    if imgs and CONFIG.get("include_images", True):
        lines.append("")
        lines.append("<font color=\"comment\">图片 %d 张：</font>" % len(imgs))
        for u in imgs[:6]:
            lines.append("<font color=\"comment\">%s</font>" % u)
    if replies and CONFIG.get("include_replies", True):
        lines.append("")
        lines.append("**回复 %d 条**" % len(replies))
        for r in replies[: CONFIG.get("reply_limit", 15)]:
            rc = clean_text(r.get("Content")).replace("\n", " ")
            rn = r.get("nick") or "匿名"
            if len(rc) > 140:
                rc = rc[:140] + "…"
            lines.append("> **%s**：%s" % (rn, rc))
        if len(replies) > CONFIG.get("reply_limit", 15):
            lines.append("> <font color=\"comment\">…还有 %d 条回复</font>"
                         % (len(replies) - CONFIG.get("reply_limit", 15)))
    lines.append("")
    if nick:
        lines.append("发帖人：%s" % nick)
    lines.append("[查看原帖](%s/t/%s)" % (BASE, tid))

    return truncate_bytes("\n".join(lines), WECOM_LIMIT)


_push_times = []


def _rate_limit():
    """企微机器人限制 20 条/分钟"""
    now = time.time()
    _push_times[:] = [t for t in _push_times if now - t < 60]
    if len(_push_times) >= 18:
        wait = 61 - (now - _push_times[0])
        if wait > 0:
            log.warning("触发企微限速，等待 %.0f 秒", wait)
            time.sleep(wait)
            _push_times[:] = [t for t in _push_times if time.time() - t < 60]
    _push_times.append(time.time())


def push_wecom(markdown):
    url = CONFIG.get("wecom_webhook", "").strip()
    if not url:
        log.error("未配置 wecom_webhook，跳过推送")
        return False
    _rate_limit()
    payload = {"msgtype": "markdown", "markdown": {"content": markdown}}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.loads(r.read().decode("utf-8", "replace"))
        if res.get("errcode") == 0:
            return True
        log.error("企微推送失败: %s", res)
        return False
    except Exception as e:  # noqa
        log.error("企微推送异常: %s", e)
        return False


# ---------------------------------------------------------------- 状态
CONFIG = {}


def load_config():
    global CONFIG
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            CONFIG = json.load(f)
    else:
        CONFIG = dict(DEFAULT_CONFIG)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, ensure_ascii=False, indent=2)
    for k, v in DEFAULT_CONFIG.items():
        CONFIG.setdefault(k, v)
    return CONFIG


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa
            pass
    return {"seen": [], "initialized": False}


def save_state(st):
    st["seen"] = st["seen"][-3000:]
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------- 单实例保护
def _pid_alive(pid):
    """可靠探活：OpenProcess 对已退出但对象未销毁的进程仍会成功，
    必须再用 GetExitCodeProcess 确认状态不是 STILL_ACTIVE。"""
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    except Exception:  # noqa
        pass
    return False


def running_pid():
    """返回当前正在运行的监控进程 PID（不含自己），没有则 None"""
    if not os.path.exists(PID_PATH):
        return None
    try:
        pid = int(open(PID_PATH, encoding="utf-8").read().strip())
    except Exception:  # noqa
        return None
    if pid and pid != os.getpid() and _pid_alive(pid):
        return pid
    return None


# ---------------------------------------------------------------- 核心
LAST_SEEN_ID = None


def archive_post(t, cid_title):
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(ARCHIVE_DIR, "%s.jsonl" % day)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(t, ensure_ascii=False) + "\n")


def run_cycle(state, cid_title, push=True, limit=None):
    global LAST_SEEN_ID
    data = fetch_list(CONFIG["cid"], 1)
    items = data.get("list") or []
    if not items:
        log.info("列表为空")
        return 0
    LAST_SEEN_ID = items[0].get("ID")

    seen = set(state["seen"])
    new_items = [it for it in items if str(it.get("ID")) not in seen]

    if not state.get("initialized"):
        log.info("首次运行：记录 %d 条现有帖子为基线，不推送", len(items))
        state["seen"].extend(str(it.get("ID")) for it in items)
        state["initialized"] = True
        save_state(state)
        if not CONFIG.get("push_on_first_run"):
            return 0

    if not new_items:
        return 0

    new_items.reverse()  # 按时间正序推送（最早的先发）
    cap = limit or CONFIG.get("max_posts_per_cycle", 12)
    if len(new_items) > cap:
        log.warning("本轮新帖 %d 条，只处理最早的 %d 条，其余下轮补", len(new_items), cap)
        new_items = new_items[:cap]

    log.info("发现 %d 条新帖", len(new_items))
    ok_count = 0
    for it in new_items:
        tid = it.get("ID")
        try:
            t = fetch_thread(tid)
        except Exception as e:  # noqa
            log.error("抓取详情失败 %s: %s", tid, e)
            t = None
        if not t:
            t = {
                "ID": tid, "title": it.get("Title"), "Content": it.get("Content"),
                "time": it.get("time"), "nick": it.get("nick"),
                "reply": [], "img": it.get("img") or [],
            }
            log.warning("%s 详情接口失败，降级用列表摘要推送", tid)

        archive_post(t, cid_title)
        md = build_wecom_markdown(t, cid_title)
        if push:
            if push_wecom(md):
                ok_count += 1
                log.info("已推送 #%s %s", tid, clean_text(t.get("title"))[:40])
            else:
                log.error("推送失败 #%s", tid)
            time.sleep(0.6)
        else:
            log.info("[dry-run] #%s %s", tid, clean_text(t.get("title"))[:40])

        state["seen"].append(str(tid))
        save_state(state)

    return ok_count


def cmd_test():
    md = ("**线报屋监控 · 测试消息**\n\n"
          "如果你看到这条消息，说明推送通道已经打通。\n\n"
          "> 监控板块：%s\n> 轮询间隔：%s 秒\n> 时间：%s"
          % (CONFIG.get("cid"), CONFIG.get("interval_sec"),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    ok = push_wecom(md)
    log.info("测试推送 %s", "成功" if ok else "失败")
    return 0 if ok else 1


def cmd_backfill(n):
    data = fetch_list(CONFIG["cid"], 1)
    items = (data.get("list") or [])[:n]
    ok = 0
    for it in reversed(items):
        try:
            t = fetch_thread(it["ID"])
        except Exception as e:  # noqa
            log.error("抓取失败 %s: %s", it["ID"], e)
            continue
        archive_post(t, data.get("title") or "")
        if push_wecom(build_wecom_markdown(t, data.get("title") or "")):
            ok += 1
            log.info("已推送 #%s", it["ID"])
        time.sleep(1.0)
    log.info("回溯推送完成 %d/%d", ok, len(items))
    return 0


def main():
    load_config()
    args = sys.argv[1:]

    if "--status" in args:
        pid = running_pid()
        if pid:
            print("监控进程 运行中 (PID %s)" % pid)
        else:
            print("监控进程 未运行")
        return 0

    if "--test" in args:
        return cmd_test()
    if "--backfill" in args:
        i = args.index("--backfill")
        n = int(args[i + 1]) if len(args) > i + 1 else 3
        return cmd_backfill(n)

    cid_title = "热门活动" if str(CONFIG["cid"]) == "3" else "线报 %s" % CONFIG["cid"]
    try:
        d = fetch_list(CONFIG["cid"], 1)
        cid_title = d.get("title") or cid_title
    except Exception:  # noqa
        pass

    if "--once" in args or "--dry" in args:
        st = load_state()
        n = run_cycle(st, cid_title, push=("--dry" not in args))
        log.info("单轮完成，%d 条", n)
        return 0

    other = running_pid()
    if other:
        log.error("已有监控进程在运行 (PID %s)，本次启动退出，避免重复推送", other)
        return 1

    log.info("=" * 50)
    log.info("线报屋监控启动 | 板块=%s(%s) | 间隔=%ss", cid_title, CONFIG["cid"],
             CONFIG["interval_sec"])
    try:
        with open(PID_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:  # noqa
        pass
    state = load_state()
    cycle = 0
    while True:
        cycle += 1
        try:
            n = run_cycle(state, cid_title)
            log.info("第 %d 轮 | 新帖 %d 条 | 列表最新 #%s | 累计跟踪 %d 条",
                     cycle, n, LAST_SEEN_ID, len(state.get("seen", [])))
        except Exception as e:  # noqa
            log.error("第 %d 轮异常: %s", cycle, e)
        time.sleep(CONFIG.get("interval_sec", 60))


if __name__ == "__main__":
    sys.exit(main() or 0)
