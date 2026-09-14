#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
线报屋 (hxm5.com) 新帖监控 -> 企业微信推送  [服务器版]

设计为 cron 每分钟执行一次：* * * * * python3 server_monitor.py --once
接口签名逆向自站点前端 app.js:
    jt = floor(now_ms / 180000)                      # 5 分钟一档
    jx = FNV1a32("<path>|<querystring>|hxm5-json-guard-v1") 转 36 进制
"""
import os
import sys
import json
import time
import re
import base64
import hashlib
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
LOCK_PATH = os.path.join(HERE, "run.lock")

BASE = "https://www.hxm5.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
SALT = "hxm5-json-guard-v1"
MASK = 0xFFFFFFFF
DIGS = "0123456789abcdefghijklmnopqrstuvwxyz"

DEFAULT_CONFIG = {
    "cid": 3,
    "wecom_webhook": "",
    "bark_key": "",
    "bark_server": "https://api.day.app",
    "bark_level": "timeSensitive",
    "max_posts_per_cycle": 12,
    "include_replies": True,
    "include_images": True,
    "push_images": True,
    "max_images_per_post": 3,
    "reply_limit": 15,
    "push_on_first_run": False,
}
CONFIG = {}
WECOM_LIMIT = 4000


def log(msg, level="INFO"):
    line = "%s [%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, msg)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa
        pass
    try:
        sys.stdout.write(line + "\n")
    except Exception:  # noqa
        pass


def rotate_log():
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 5 * 1024 * 1024:
            if os.path.exists(LOG_PATH + ".1"):
                os.remove(LOG_PATH + ".1")
            os.rename(LOG_PATH, LOG_PATH + ".1")
    except Exception:  # noqa
        pass


# ------------------------------------------------------------ 签名与请求
def _b36(n):
    if n == 0:
        return "0"
    s = ""
    while n:
        s = DIGS[n % 36] + s
        n //= 36
    return s


def _fnv1a(s):
    t = 2166136261
    for ch in s:
        t ^= ord(ch)
        t = (t * 16777619) & MASK
    return _b36(t)


def _signed_body(path, params):
    p = dict(params)
    p["jt"] = int(time.time() * 1000 // 180000)
    qs = urllib.parse.urlencode(p, quote_via=urllib.parse.quote)
    return qs + "&jx=" + _fnv1a("%s|%s|%s" % (path, qs, SALT))


def api(path, params, retries=3):
    body = _signed_body(path, params)
    last = None
    for i in range(retries):
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
                raise RuntimeError("code=%s msg=%s" % (data.get("code"), data.get("msg")))
            return data.get("data")
        except Exception as e:  # noqa
            last = e
            if i < retries - 1:
                time.sleep(2)
    raise RuntimeError("request %s failed: %s" % (path, last))


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


# ------------------------------------------------------------ 文本处理
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
    return b[:limit].decode("utf-8", "ignore") + "\n\n…(过长已截断)"


# ------------------------------------------------------------ 推送
def build_markdown(t, cid_title):
    title = clean_text(t.get("title") or t.get("word") or "(无标题)")
    content = clean_text(t.get("Content"))
    nick = t.get("nick") or ""
    ttime = t.get("time") or ""
    tid = t.get("ID")
    replies = t.get("reply") or []
    imgs = t.get("img") or []
    L = []
    L.append("**线报屋 · %s**  <font color=\"comment\">%s</font>" % (cid_title, ttime))
    L.append("")
    L.append("### %s" % title)
    if content:
        L.append("")
        L.append(content)
    if imgs and CONFIG.get("include_images", True):
        L.append("")
        L.append("<font color=\"comment\">图片 %d 张：</font>" % len(imgs))
        for u in imgs[:6]:
            L.append("<font color=\"comment\">%s</font>" % u)
    if replies and CONFIG.get("include_replies", True):
        lim = CONFIG.get("reply_limit", 15)
        L.append("")
        L.append("**回复 %d 条**" % len(replies))
        for r in replies[:lim]:
            rc = clean_text(r.get("Content")).replace("\n", " ")
            if len(rc) > 140:
                rc = rc[:140] + "…"
            L.append("> **%s**：%s" % (r.get("nick") or "匿名", rc))
        if len(replies) > lim:
            L.append("> <font color=\"comment\">…还有 %d 条</font>" % (len(replies) - lim))
    L.append("")
    if nick:
        L.append("发帖人：%s" % nick)
    L.append("[查看原帖](%s/t/%s)" % (BASE, tid))
    return truncate_bytes("\n".join(L), WECOM_LIMIT)


_SENT_TS = []


def _rate_limit(limit=18):
    """企微限制 20 条/分钟，留余量做滑动窗口限速"""
    now = time.time()
    while _SENT_TS and now - _SENT_TS[0] > 60:
        _SENT_TS.pop(0)
    if len(_SENT_TS) >= limit:
        wait = 61 - (now - _SENT_TS[0])
        if wait > 0:
            log("触发企微限速，等待 %.0f 秒" % wait, "WARN")
            time.sleep(wait)
            del _SENT_TS[:]
    _SENT_TS.append(time.time())


def push_wecom(markdown):
    url = CONFIG.get("wecom_webhook", "").strip()
    if not url:
        log("未配置 wecom_webhook", "ERROR")
        return False
    _rate_limit()
    payload = {"msgtype": "markdown", "markdown": {"content": markdown}}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.loads(r.read().decode("utf-8", "replace"))
        if res.get("errcode") == 0:
            return True
        log("企微推送失败: %s" % res, "ERROR")
        return False
    except Exception as e:  # noqa
        log("企微推送异常: %s" % e, "ERROR")
        return False


def push_wecom_image(img_url):
    """下载图片，以企微 image 类型推送（每张一条消息）"""
    url = CONFIG.get("wecom_webhook", "").strip()
    if not url:
        return False
    try:
        req = urllib.request.Request(img_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
    except Exception as e:  # noqa
        log("图片下载失败 %s: %s" % (img_url, e), "WARN")
        return False
    if not raw:
        return False
    if len(raw) > 2 * 1024 * 1024:
        log("图片 %.1fMB 超企微 2MB 上限，跳过" % (len(raw) / 1048576.0), "WARN")
        return False
    _rate_limit()
    payload = {
        "msgtype": "image",
        "image": {
            "base64": base64.b64encode(raw).decode(),
            "md5": hashlib.md5(raw).hexdigest(),
        },
    }
    data = json.dumps(payload).encode("utf-8")
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"}), timeout=30) as r:
            res = json.loads(r.read().decode("utf-8", "replace"))
        if res.get("errcode") == 0:
            return True
        log("企微发图失败: %s" % res, "ERROR")
        return False
    except Exception as e:  # noqa
        log("企微发图异常: %s" % e, "ERROR")
        return False


# ------------------------------------------------------------ Bark (iOS)
def build_bark_message(t):
    """Bark 通知标题要短，正文也不宜过长（iOS 会折叠）"""
    title = clean_text(t.get("title") or t.get("word") or "(无标题)")
    content = clean_text(t.get("Content"))
    replies = t.get("reply") or []
    nick = t.get("nick") or ""
    ttime = t.get("time") or ""
    tid = t.get("ID")

    n_title = "[线报屋] " + title
    if len(n_title) > 55:
        n_title = n_title[:55] + "…"

    parts = []
    if content:
        parts.append(content)
    if replies and CONFIG.get("include_replies", True):
        lim = min(CONFIG.get("reply_limit", 15), 5)
        parts.append("")
        parts.append("—— 回复 %d 条 ——" % len(replies))
        for r in replies[:lim]:
            rc = clean_text(r.get("Content")).replace("\n", " ")
            if len(rc) > 80:
                rc = rc[:80] + "…"
            parts.append("%s：%s" % (r.get("nick") or "匿名", rc))
    if nick:
        parts.append("")
        parts.append("发帖人：%s  %s" % (nick, ttime))

    body = "\n".join(parts).strip()
    if len(body) > 700:
        body = body[:700] + "…"
    return n_title, body, "%s/t/%s" % (BASE, tid)


def push_bark(title, body, url="", icon=""):
    key = (CONFIG.get("bark_key") or "").strip()
    if not key:
        return None
    if key.startswith("http"):
        endpoint = key.rstrip("/") + "/"
    else:
        endpoint = (CONFIG.get("bark_server") or "https://api.day.app").rstrip("/") + "/" + key
    payload = {
        "title": title,
        "body": body,
        "group": "线报屋",
        "level": CONFIG.get("bark_level", "timeSensitive"),
        "isArchive": 1,
    }
    if url:
        payload["url"] = url
    if icon:
        payload["icon"] = icon
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(endpoint, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.loads(r.read().decode("utf-8", "replace"))
        if res.get("code") == 200:
            return True
        log("Bark 推送失败: %s" % res, "ERROR")
        return False
    except Exception as e:  # noqa
        log("Bark 推送异常: %s" % e, "ERROR")
        return False


def push_all(t, cid_title):
    """推送一条帖子到所有已启用渠道（文字 + 图片）"""
    detail = []
    any_ok = False
    imgs = t.get("img") or []
    has_wecom = bool((CONFIG.get("wecom_webhook") or "").strip())
    has_bark = bool((CONFIG.get("bark_key") or "").strip())

    if has_wecom:
        ok = push_wecom(build_markdown(t, cid_title))
        detail.append("企微=%s" % ("ok" if ok else "FAIL"))
        any_ok = any_ok or ok

    if has_bark:
        bt, bb, bu = build_bark_message(t)
        ok = push_bark(bt, bb, bu, icon=(imgs[0] if imgs else ""))
        detail.append("Bark=%s" % ("ok" if ok else "FAIL"))
        any_ok = any_ok or ok

    # 图片：企微 image 类型逐张推送（Bark 通知无法内嵌图片）
    if has_wecom and imgs and CONFIG.get("push_images", True):
        n = min(len(imgs), CONFIG.get("max_images_per_post", 3))
        sent = 0
        for u in imgs[:n]:
            if push_wecom_image(u):
                sent += 1
        detail.append("图片=%d/%d" % (sent, len(imgs)))
        any_ok = any_ok or sent > 0

    if not detail:
        log("没有启用任何推送渠道", "ERROR")
        return False
    log("推送结果 " + " ".join(detail), "INFO" if any_ok else "ERROR")
    return any_ok


# ------------------------------------------------------------ 状态
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


def archive_post(t):
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    with open(os.path.join(ARCHIVE_DIR, "%s.jsonl" % day), "a", encoding="utf-8") as f:
        f.write(json.dumps(t, ensure_ascii=False) + "\n")


# ------------------------------------------------------------ 锁
def acquire_lock():
    now = time.time()
    if os.path.exists(LOCK_PATH):
        try:
            ts = float(open(LOCK_PATH).read().strip())
            if now - ts < 150:
                return False
        except Exception:  # noqa
            pass
    with open(LOCK_PATH, "w") as f:
        f.write(str(now))
    return True


# ------------------------------------------------------------ 主流程
def run_once(dry=False):
    cfg = load_config()
    state = load_state()
    data = fetch_list(cfg["cid"], 1)
    items = data.get("list") or []
    if not items:
        log("列表为空")
        return 0
    cid_title = data.get("title") or ("线报 %s" % cfg["cid"])
    seen = set(state["seen"])
    new_items = [it for it in items if str(it.get("ID")) not in seen]

    if not state.get("initialized"):
        state["seen"].extend(str(it.get("ID")) for it in items)
        state["initialized"] = True
        save_state(state)
        log("首次运行：记录 %d 条为基线，不推送" % len(items))
        if not cfg.get("push_on_first_run"):
            return 0

    if not new_items:
        log("无新帖（列表最新 #%s）" % items[0].get("ID"))
        return 0

    new_items.reverse()
    cap = cfg.get("max_posts_per_cycle", 12)
    if len(new_items) > cap:
        log("本轮新帖 %d 条，只处理最早 %d 条" % (len(new_items), cap), "WARN")
        new_items = new_items[:cap]

    log("发现 %d 条新帖" % len(new_items))
    ok = 0
    for it in new_items:
        tid = it.get("ID")
        try:
            t = fetch_thread(tid)
        except Exception as e:  # noqa
            log("抓详情失败 %s: %s" % (tid, e), "ERROR")
            t = {"ID": tid, "title": it.get("Title"), "Content": it.get("Content"),
                 "time": it.get("time"), "nick": it.get("nick"), "reply": [],
                 "img": it.get("img") or []}
        archive_post(t)
        if dry:
            log("[dry] #%s %s" % (tid, clean_text(t.get("title"))[:40]))
        elif push_all(t, cid_title):
            ok += 1
            log("已推送 #%s %s" % (tid, clean_text(t.get("title"))[:40]))
        else:
            log("推送失败 #%s" % tid, "ERROR")
        state["seen"].append(str(tid))
        save_state(state)
        time.sleep(0.6)
    return ok


def main():
    rotate_log()
    args = sys.argv[1:]
    if not acquire_lock():
        log("上一轮仍在运行，跳过本次", "WARN")
        return 0
    try:
        if "--test" in args:
            cfg = load_config()
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ok = False
            if (cfg.get("wecom_webhook") or "").strip():
                if push_wecom("**线报屋监控 · 推送测试**\n\n"
                              "如果你看到这条消息，说明企微通道已打通。\n\n"
                              "> 监控板块：%s\n> 时间：%s" % (cfg.get("cid"), ts)):
                    ok = True
            if (cfg.get("bark_key") or "").strip():
                if push_bark("[线报屋] 推送测试",
                             "如果你看到这条通知，说明 Bark 通道已打通。\n\n"
                             "板块：%s\n时间：%s\n级别：%s"
                             % (cfg.get("cid"), ts, cfg.get("bark_level"))):
                    ok = True
            log("测试推送 %s" % ("成功" if ok else "失败"))
            return 0 if ok else 1
        if "--barktest" in args:
            load_config()
            ok = push_bark("[线报屋] Bark 通道测试",
                           "这是一条来自腾讯云服务器的测试通知。\n"
                           "级别：%s\n时间：%s"
                           % (CONFIG.get("bark_level"),
                              datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            log("Bark 测试推送 %s" % ("成功" if ok else "失败"))
            return 0 if ok else 1
        if "--imgtest" in args:
            cfg = load_config()
            d = fetch_list(cfg["cid"], 1)
            for it in (d.get("list") or []):
                imgs = it.get("img") or []
                if imgs:
                    log("拿帖子 #%s 的 %d 张图做测试" % (it.get("ID"), len(imgs)))
                    ok = push_wecom_image(imgs[0])
                    log("图片推送测试 %s" % ("成功" if ok else "失败"))
                    return 0 if ok else 1
            log("当前列表里没有带图的帖子", "WARN")
            return 1
        if "--backfill" in args:
            i = args.index("--backfill")
            n = int(args[i + 1]) if len(args) > i + 1 else 3
            cfg = load_config()
            d = fetch_list(cfg["cid"], 1)
            title = d.get("title") or ""
            cnt = 0
            for it in reversed((d.get("list") or [])[:n]):
                t = fetch_thread(it["ID"])
                archive_post(t)
                if push_all(t, title):
                    cnt += 1
                time.sleep(1)
            log("回溯完成 %d/%d" % (cnt, n))
            return 0
        ok = run_once(dry=("--dry" in args))
        return 0
    finally:
        try:
            os.remove(LOCK_PATH)
        except Exception:  # noqa
            pass


if __name__ == "__main__":
    sys.exit(main() or 0)
