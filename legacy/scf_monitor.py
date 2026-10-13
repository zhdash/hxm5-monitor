# -*- coding: utf-8 -*-
"""
线报屋 (hxm5.com) 新帖监控 -> 企业微信 / Bark 推送   [腾讯云云函数 SCF 版]

入口：main_handler(event, context)
触发：定时触发器，每分钟一次（Cron: 0 * * * * * *）

配置全部通过「函数配置 -> 环境变量」注入，改配置不需要重新部署：
    HXM5_CID              板块 ID（3=热门活动，0=最新线报）
    HXM5_WECOM_WEBHOOK    企业微信消息推送 Webhook（留空则不推企微）
    HXM5_BARK_KEY         Bark 的 key（留空则不推 Bark）
    HXM5_BARK_LEVEL       Bark 级别：timeSensitive / active / critical
    HXM5_MAX_IMAGES       每帖最多推送几张图，默认 3
    HXM5_FALLBACK_MIN     状态丢失时的兜底时间窗（分钟），默认 3

⚠️ 关于状态：云函数实例可能被回收，/tmp 不保证长期存在。
   本实现对此做了容错 —— 状态在时按帖子 ID 精确去重；状态丢失时退化为
   「只处理最近 N 分钟内的帖子」，保证不漏推（最坏情况是重复推几条）。
"""
import os
import json
import time
import re
import base64
import hashlib
import html as htmllib
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime

STATE_PATH = "/tmp/hxm5_state.json"
LOG_PATH = "/tmp/hxm5.log"

BASE = "https://www.hxm5.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
SALT = "hxm5-json-guard-v1"
MASK = 0xFFFFFFFF
DIGS = "0123456789abcdefghijklmnopqrstuvwxyz"
WECOM_LIMIT = 4000

DEFAULTS = {
    "cid": "3",
    "wecom_webhook": "",
    "bark_key": "",
    "bark_server": "https://api.day.app",
    "bark_level": "timeSensitive",
    "max_images": "3",
    "reply_limit": "15",
    "fallback_min": "3",
    # 去重状态存 COS（不配则退回 /tmp，会在实例回收后重复推送）
    "cos_bucket": "",
    "cos_region": "ap-guangzhou",
    "cos_secret_id": "",
    "cos_secret_key": "",
    "cos_key": "state/hxm5_state.json",
}


def log(msg):
    line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:  # 便于当轮次内排查（/tmp 可能被回收，仅作辅助）
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def cfg(key):
    v = os.environ.get("HXM5_" + key.upper())
    if v is None or v == "":
        v = DEFAULTS.get(key, "")
    return v


# ------------------------------------------------------------ 签名 / 请求
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
        except Exception as e:
            last = e
            if i < retries - 1:
                time.sleep(1)
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


def parse_post_time(s):
    """把 '2026-09-14 10:43' 转成时间戳，失败返回 None"""
    try:
        return time.mktime(datetime.strptime(s.strip(), "%Y-%m-%d %H:%M").timetuple())
    except Exception:
        return None


# ------------------------------------------------------------ 推送
def build_wecom_markdown(t, cid_title):
    title = clean_text(t.get("title") or t.get("word") or "(无标题)")
    content = clean_text(t.get("Content"))
    nick = t.get("nick") or ""
    ttime = t.get("time") or ""
    tid = t.get("ID")
    replies = t.get("reply") or []
    imgs = t.get("img") or []
    L = ["**线报屋 · %s**  <font color=\"comment\">%s</font>" % (cid_title, ttime), ""]
    L.append("### %s" % title)
    if content:
        L += ["", content]
    if imgs:
        L += ["", "<font color=\"comment\">图片 %d 张（见后续图片消息）</font>" % len(imgs)]
    if replies:
        lim = int(cfg("reply_limit"))
        L += ["", "**回复 %d 条**" % len(replies)]
        for r in replies[:lim]:
            rc = clean_text(r.get("Content")).replace("\n", " ")
            if len(rc) > 140:
                rc = rc[:140] + "…"
            L.append("> **%s**：%s" % (r.get("nick") or "匿名", rc))
        if len(replies) > lim:
            L.append("> <font color=\"comment\">…还有 %d 条</font>" % (len(replies) - lim))
    if nick:
        L += ["", "发帖人：%s" % nick]
    L.append("[查看原帖](%s/t/%s)" % (BASE, tid))
    return truncate_bytes("\n".join(L), WECOM_LIMIT)


def build_bark_message(t):
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
    if replies:
        lim = min(int(cfg("reply_limit")), 5)
        parts += ["", "—— 回复 %d 条 ——" % len(replies)]
        for r in replies[:lim]:
            rc = clean_text(r.get("Content")).replace("\n", " ")
            if len(rc) > 80:
                rc = rc[:80] + "…"
            parts.append("%s：%s" % (r.get("nick") or "匿名", rc))
    if nick:
        parts += ["", "发帖人：%s  %s" % (nick, ttime)]
    body = "\n".join(parts).strip()
    if len(body) > 700:
        body = body[:700] + "…"
    return n_title, body, "%s/t/%s" % (BASE, tid)


def _post_json(url, payload, timeout=25):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def push_wecom(markdown):
    url = cfg("wecom_webhook").strip()
    if not url:
        return None
    try:
        res = _post_json(url, {"msgtype": "markdown", "markdown": {"content": markdown}})
        if res.get("errcode") == 0:
            return True
        log("企微推送失败: %s" % res)
        return False
    except Exception as e:
        log("企微推送异常: %s" % e)
        return False


def push_wecom_image(img_url):
    url = cfg("wecom_webhook").strip()
    if not url:
        return False
    try:
        req = urllib.request.Request(img_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
    except Exception as e:
        log("图片下载失败 %s: %s" % (img_url, e))
        return False
    if not raw:
        return False
    if len(raw) > 2 * 1024 * 1024:
        log("图片 %.1fMB 超企微 2MB 上限，跳过" % (len(raw) / 1048576.0))
        return False
    payload = {"msgtype": "image", "image": {
        "base64": base64.b64encode(raw).decode(),
        "md5": hashlib.md5(raw).hexdigest()}}
    try:
        res = _post_json(url, payload, timeout=30)
        if res.get("errcode") == 0:
            return True
        log("企微发图失败: %s" % res)
        return False
    except Exception as e:
        log("企微发图异常: %s" % e)
        return False


def push_bark(title, body, url="", icon=""):
    key = cfg("bark_key").strip()
    if not key:
        return None
    if key.startswith("http"):
        endpoint = key.rstrip("/") + "/"
    else:
        endpoint = cfg("bark_server").rstrip("/") + "/" + key
    payload = {"title": title, "body": body, "group": "线报屋",
               "level": cfg("bark_level"), "isArchive": 1}
    if url:
        payload["url"] = url
    if icon:
        payload["icon"] = icon
    try:
        res = _post_json(endpoint, payload)
        if res.get("code") == 200:
            return True
        log("Bark 推送失败: %s" % res)
        return False
    except Exception as e:
        log("Bark 推送异常: %s" % e)
        return False


def push_all(t, cid_title):
    detail = []
    any_ok = False
    imgs = t.get("img") or []
    has_wecom = bool(cfg("wecom_webhook").strip())
    has_bark = bool(cfg("bark_key").strip())

    if has_wecom:
        ok = push_wecom(build_wecom_markdown(t, cid_title))
        detail.append("企微=%s" % ("ok" if ok else "FAIL"))
        any_ok = any_ok or bool(ok)

    if has_bark:
        bt, bb, bu = build_bark_message(t)
        ok = push_bark(bt, bb, bu, icon=(imgs[0] if imgs else ""))
        detail.append("Bark=%s" % ("ok" if ok else "FAIL"))
        any_ok = any_ok or bool(ok)

    if has_wecom and imgs:
        n = min(len(imgs), int(cfg("max_images")))
        sent = 0
        for u in imgs[:n]:
            if push_wecom_image(u):
                sent += 1
        detail.append("图片=%d/%d" % (sent, len(imgs)))
        any_ok = any_ok or sent > 0

    log("推送结果 " + (" ".join(detail) if detail else "无可用渠道"))
    return any_ok


# ------------------------------------------------------------ 状态（COS 持久化）
from cos_state import CosState

_cos = None


def _get_cos():
    """惰性初始化 COS 客户端；未配置则返回 None"""
    global _cos
    if _cos is None:
        bucket = cfg("cos_bucket").strip()
        sid = cfg("cos_secret_id").strip()
        skey = cfg("cos_secret_key").strip()
        if bucket and sid and skey:
            _cos = CosState(sid, skey, cfg("cos_region"), bucket, cfg("cos_key"))
    return _cos


def load_state():
    """优先从 COS 读；COS 不可用时退回 /tmp。返回 None 表示彻底没有状态"""
    cs = _get_cos()
    if cs is not None:
        st = cs.load()
        if st is not None:
            log("状态来自 COS（已记录 %d 条）" % len(st.get("seen") or []))
            return st
        log("COS 中暂无状态文件")
        return None
    # 兜底：/tmp（不可靠，仅当未配置 COS 时用）
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict) and "seen" in st:
            return st
    except Exception:
        pass
    return None


def save_state(st):
    cs = _get_cos()
    if cs is not None:
        if cs.save(st):
            return
        log("COS 状态保存失败，尝试写 /tmp")
    try:
        st["seen"] = st["seen"][-2000:]
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        log("状态保存失败: %s" % e)


# ------------------------------------------------------------ 主流程
MAX_CYCLE_SEC = 50          # 单轮时间预算，留 10s 余量给 60s 超时
MAX_PUSH_PER_CYCLE = 6      # 单轮最多推几条（每条约 3-4s）


def run_once():
    t_start = time.time()
    cid = cfg("cid")
    state = load_state()
    data = fetch_list(cid, 1)
    items = data.get("list") or []
    if not items:
        log("列表为空")
        return
    cid_title = data.get("title") or ("线报 %s" % cid)

    if state is None:
        # 首次运行 / 状态彻底丢失：退化为时间窗口模式
        window = int(cfg("fallback_min")) * 60
        now = time.time()
        new_items = []
        for it in items:
            ts = parse_post_time(it.get("time") or "")
            if ts and (now - ts) <= window:
                new_items.append(it)
        log("首次运行/状态丢失，时间窗模式（%s 分钟内 %d 条）" % (cfg("fallback_min"), len(new_items)))
        state = {"seen": [str(it.get("ID")) for it in items]}
        # 立刻落盘，避免下一轮又变成"状态丢失"
        save_state(state)
    else:
        seen = set(state["seen"])
        new_items = [it for it in items if str(it.get("ID")) not in seen]
        if not new_items:
            log("无新帖（列表最新 #%s）" % items[0].get("ID"))
            return
        log("发现 %d 条新帖" % len(new_items))

    # 按时间正序推送；单轮限量，避免超出云函数执行时长（剩余的下一轮继续）
    new_items.reverse()
    if len(new_items) > MAX_PUSH_PER_CYCLE:
        log("本轮新帖 %d 条，只处理最早 %d 条，其余下轮继续" % (len(new_items), MAX_PUSH_PER_CYCLE))
        new_items = new_items[:MAX_PUSH_PER_CYCLE]

    pushed = 0
    for it in new_items:
        if time.time() - t_start > MAX_CYCLE_SEC:
            log("已达单轮时间预算 %ds，剩余 %d 条下轮继续" % (MAX_CYCLE_SEC, len(new_items) - pushed))
            break
        tid = it.get("ID")
        try:
            t = fetch_thread(tid)
        except Exception as e:
            log("抓详情失败 %s: %s" % (tid, e))
            t = {"ID": tid, "title": it.get("Title"), "Content": it.get("Content"),
                 "time": it.get("time"), "nick": it.get("nick"),
                 "reply": [], "img": it.get("img") or []}
        push_all(t, cid_title)
        # 只有推送过才记账，保证失败/超时不会漏帖
        if str(tid) not in state["seen"]:
            state["seen"].append(str(tid))
        save_state(state)
        pushed += 1
        time.sleep(0.3)
    log("本轮推送 %d 条" % pushed)


def main_handler(event, context):
    t0 = time.time()
    try:
        run_once()
    except Exception as e:
        log("执行异常: %s" % e)
        return "error: %s" % e
    log("本轮完成，耗时 %.1fs" % (time.time() - t0))
    return "ok"
