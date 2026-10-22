#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
线报屋 (hxm5.com) 新帖监控 -> 企业微信推送  [GitHub Actions 版]

与服务器版的区别：
  - 无状态运行：GitHub Actions 每次跑都是全新虚拟机，去重状态由 workflow
    负责从仓库里 checkout / 跑完 commit 回去（见 .github/workflows/monitor.yml）
  - 配置全部走环境变量（GitHub Secrets / Variables），不落盘明文
  - 单次执行即退出，不常驻

本地调试：
  python3 actions_monitor.py --once       # 跑一轮就退出（Actions 用的就是这个）
  python3 actions_monitor.py --dry        # 空跑：只打印不推送
  python3 actions_monitor.py --test       # 发一条测试消息
  python3 actions_monitor.py --backfill N # 回溯最近 N 条

退出码：
  0 = 正常（含"无新帖"）
  2 = 一个推送渠道都没配（脚本无法工作）
  3 = 有帖子要推，但一条都没成功
"""
import os
import sys
import json
import time
import re
import base64
import hashlib
import hmac
import html as htmllib
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.environ.get("HXM5_STATE_PATH") or os.path.join(HERE, "state.json")
ARCHIVE_DIR = os.path.join(HERE, "archive")
# 归档开关。Actions 版跑在一次性虚拟机上，archive/ 被 .gitignore 挡住 →
# 写了随虚拟机销毁，纯无效 IO。默认关闭；本地想留档时设 HXM5_ARCHIVE=1。
ARCHIVE_ENABLED = (os.environ.get("HXM5_ARCHIVE") or "").strip().lower() in (
    "1", "true", "yes", "on")

BASE = "https://www.hxm5.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
SALT = "hxm5-json-guard-v1"
MASK = 0xFFFFFFFF
DIGS = "0123456789abcdefghijklmnopqrstuvwxyz"
CST = timezone(timedelta(hours=8))
WECOM_LIMIT = 4000
# 钉钉 markdown 正文的实际上限远高于 4000（多方实现按 20000 处理，
# 官方自定义机器人文档未给出该字段的确切数值）→ 这里留足余量取 18000。
# 以前误用 4000，等于白白浪费了 80% 的空间，长帖频繁触发截断。
DT_LIMIT = 18000

# ---- 内容指纹去重（与帖子 ID 无关）----
# 只靠帖子 ID 去重是不够的，有三类情况会让「同一内容」被推第二次：
#   ① 同一件事被不同人重发（ID 不同、正文几乎一样）
#   ② 状态文件丢失/回滚后，同一个 ID 被重新当成新帖
#   ③ 推送其实已送达，但 HTTP 响应超时被误判为失败 → 重试
# 指纹由「标题+正文」算出，与 ID 无关，三类情况都能兜住。
FP_TTL_SEC = 7 * 86400     # 同一内容的去重窗口：7 天
MAX_SENT_FP = 2000         # 指纹表最多留多少条（只保留最新）
FP_MIN_CHARS = 8           # 内容少于这个字数不做指纹去重，避免把不同短帖误判成同一篇
SEEN_KEEP = 5000           # seen 保留条数。原为 3000：一旦超出，最老的会被丢弃，
                           # 而"热门活动"列表里长期挂着的老帖就可能被重新推送。

# 退出码：只有真正坏掉时才返回非 0，让 GitHub Actions 能把这一轮标红。
# 以前所有异常路径都 return 0，导致「一条都不推」时 run 依然显示绿色成功。
EXIT_OK = 0
EXIT_NO_CHANNEL = 2          # 一个推送渠道都没配，等于脚本根本没干活
EXIT_ALL_PUSH_FAILED = 3     # 有帖子要推，但一条都没推成功

# 单条帖子最多尝试推送次数。到上限才放弃并标记已读 ——
# 必须有上限，否则一条永久失败的帖子会一直堵在队首（max_posts_per_cycle 有配额）。
MAX_PUSH_RETRY = 3


# ------------------------------------------------------------ 配置
def cfg(key, default=None, cast=str):
    raw = os.environ.get("HXM5_" + key.upper())
    if raw is None or raw == "":
        return default
    try:
        if cast is bool:
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        return cast(raw)
    except Exception:  # noqa
        return default


CONFIG = {}

# 默认值的【唯一出处】—— 不要在调用点再写一遍，否则两边会悄悄不一致
# （push_images 曾经就是 load_config 写 False、push_all 写 True，是个维护陷阱）
DEFAULT_PUSH_IMAGES = False


def load_config():
    global CONFIG
    CONFIG = {
        "cid": cfg("cid", 3, int),
        "wecom_webhook": cfg("wecom_webhook", ""),
        "dingtalk_webhook": cfg("dingtalk_webhook", ""),
        "dingtalk_secret": cfg("dingtalk_secret", ""),
        "bark_key": cfg("bark_key", ""),
        "bark_server": cfg("bark_server", "https://api.day.app"),
        "bark_level": cfg("bark_level", "timeSensitive"),
        "max_posts_per_cycle": cfg("max_posts_per_cycle", 12, int),
        "include_replies": cfg("include_replies", True, bool),
        "include_images": cfg("include_images", True, bool),
        "push_images": cfg("push_images", DEFAULT_PUSH_IMAGES, bool),
        "max_images_per_post": cfg("max_images_per_post", 3, int),
        "reply_limit": cfg("reply_limit", 15, int),
        "push_on_first_run": cfg("push_on_first_run", False, bool),
    }
    return CONFIG


def log(msg, level="INFO"):
    line = "%s [%s] %s" % (datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"), level, msg)
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
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


API_PATH = "/api"


def _signed_body(params):
    """生成带签名的 POST body。

    ⭐ 2026-10-22 站点改版，签名算法 **变了**：

        旧：jx = FNV1a32("<path>|<querystring>|hxm5-json-guard-v1")
        新：jx = FNV1a32("/api|<完整参数串含 jt>|hxm5-json-guard-v1")

    新算法只认字面量 "/api" + 整个 body 参数串（**含 jt**）+ 盐，
    不再包含 path 和 querystring。**参数键的插入顺序会直接影响签名**，
    所以 params 的键顺序必须与站点前端保持一致，不能随意调整。
    """
    p = dict(params)
    p["jt"] = str(int(time.time() * 1000) // 180000)
    qs = urllib.parse.urlencode(p, quote_via=urllib.parse.quote)
    return qs + "&jx=" + _fnv1a("%s|%s|%s" % (API_PATH, qs, SALT))


def api(params, query="", referer=None, retries=3):
    """POST /api?<query>，body 为签名后的参数串。"""
    body = _signed_body(params)
    url = BASE + API_PATH + (("?" + query) if query else "")
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST",
                                         headers={
                "User-Agent": UA,
                "Content-Type": "application/x-www-form-urlencoded",
                "X-HXM5-JSON": "1",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": referer or (BASE + "/"),
                "Origin": BASE,
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
    raise RuntimeError("request %s?%s failed: %s" % (API_PATH, query, last))


def fetch_list(cid, page=1):
    cid_s, page_s = str(cid), str(page)
    # 键顺序 (r, type, cid, page, view) 必须与站点前端一致 —— 会影响签名
    params = {
        "r": "list",
        "type": "index" if cid_s == "0" else "xianbao",
        "cid": cid_s,
        "page": page_s,
        "view": "cards" if cid_s in ("2", "3") else "rows",
    }
    return api(params, "cate=%s&page=%s" % (cid_s, page_s),
               referer=BASE + "/xianbao/%s/" % cid_s)


def fetch_thread(tid):
    tid_s = str(tid)
    return api({"r": "thread", "id": tid_s}, "tid=%s" % tid_s,
               referer=BASE + "/t/%s/" % tid_s)


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


TRUNC_MARK = "\n\n…(过长已截断)"


def truncate_bytes(s, limit):
    """按 UTF-8 字节截断，保证【结果连截断标记一起也不超过 limit 字节】。

    ⚠️ 以前是把正文截到 limit 再拼标记，于是实际载荷 = limit + 标记长度，
    会悄悄超出平台上限（实测 4021 > 4000）。所以这里先把标记的额度扣掉。
    """
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    mk = TRUNC_MARK.encode("utf-8")
    if limit <= len(mk):
        # 额度连截断标记都放不下 → 不加标记，硬截断，仍然保证不超上限
        return b[:limit].decode("utf-8", "ignore")
    # "ignore" 保证多字节字符不会被劈成半个（否则平台可能整条拒收）
    return b[:limit - len(mk)].decode("utf-8", "ignore") + TRUNC_MARK


def content_fingerprint(t):
    """算「帖子内容」的指纹，用于跨 ID 去重。内容过短时返回 ""（放弃去重）。

    只对规范化后的「标题+正文」取哈希：空格/全角空格/大小写都不影响，
    但正文本身的差异会被如实反映。**不包含帖子 ID、发帖人、时间**，
    所以同一条线报被别人重发时，指纹相同 → 会被拦下。
    """
    title = clean_text(t.get("title") or t.get("word") or "")
    body = clean_text(t.get("Content") or "")
    raw = re.sub(r"[\s\u3000]+", "", title + "|" + body).lower()
    if len(raw) < FP_MIN_CHARS:
        return ""
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


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
    # ⭐ 原帖链接【不参与截断】：先给它预留额度，再截正文。
    #   以前是先拼全文再从尾部切，长帖会把链接一起切掉 → 推送里没有原帖入口。
    tail = "\n\n[查看原帖](%s/t/%s)" % (BASE, tid)
    budget = WECOM_LIMIT - len(tail.encode("utf-8"))
    return truncate_bytes("\n".join(L), budget) + tail


def push_wecom(markdown):
    url = CONFIG.get("wecom_webhook", "").strip()
    if not url:
        log("未配置 HXM5_WECOM_WEBHOOK", "ERROR")
        return False
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


# ------------------------------------------------------------ 钉钉
def _dingtalk_sign(secret):
    """加签模式：timestamp + HMAC-SHA256(secret, "ts\nsecret")"""
    ts = str(round(time.time() * 1000))
    s2s = "%s\n%s" % (ts, secret)
    h = hmac.new(secret.encode("utf-8"), s2s.encode("utf-8"),
                 digestmod=hashlib.sha256).digest()
    return ts, urllib.parse.quote_plus(base64.b64encode(h))


def build_dingtalk(t, cid_title):
    """钉钉 markdown：不支持 font 颜色/表格；图片走 ![alt](url)。

    链接必须用代码块包住 —— 钉钉会把裸 URL 自动链接化，一旦链接化就
    「点击被安全网关拦 + 复制出来是 URL 编码」两个坑同时中招，
    只有代码块里的原文才能被长按干净复制（2026-09-17 实测结论）。
    """
    title = clean_text(t.get("title") or t.get("word") or "(无标题)")
    content = clean_text(t.get("Content"))
    nick = t.get("nick") or ""
    ttime = t.get("time") or ""
    tid = t.get("ID")
    replies = t.get("reply") or []
    imgs = t.get("img") or []

    n_title = "线报屋·%s" % title
    if len(n_title) > 40:
        n_title = n_title[:40] + "…"

    L = ["### %s" % title]
    if ttime or nick:
        L.append("> %s %s" % (ttime, nick))
    if content:
        L.append(content)
    if imgs and CONFIG.get("include_images", True):
        L.append("")
        for u in imgs[:6]:
            L.append("![图片](%s)" % u)
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
            L.append("> …还有 %d 条" % (len(replies) - lim))
    # ⭐ 链接块（含 ``` 围栏）最后拼，且【先给它预留额度】再截断正文。
    #   以前是"先拼全文、再从尾部截断"，于是长帖会把链接连同 ``` 闭合围栏一起切掉 ——
    #   恰好把 09-17 费力解决的「钉钉链接必须用代码块」这个成果静默废掉。
    tail = "\n".join(["", "原帖：", "```", "%s/t/%s" % (BASE, tid), "```"])
    budget = DT_LIMIT - len(tail.encode("utf-8"))
    return n_title, truncate_bytes("\n".join(L), budget) + tail


def push_dingtalk(title, text):
    url = (CONFIG.get("dingtalk_webhook") or "").strip()
    if not url:
        return False
    secret = (CONFIG.get("dingtalk_secret") or "").strip()
    if secret:
        ts, sign = _dingtalk_sign(secret)
        url = "%s%stimestamp=%s&sign=%s" % (
            url, "&" if "?" in url else "?", ts, sign)
    payload = {"msgtype": "markdown",
               "markdown": {"title": title, "text": text}}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.loads(r.read().decode("utf-8", "replace"))
        if res.get("errcode") == 0:
            return True
        log("钉钉推送失败: %s" % res, "ERROR")
        return False
    except Exception as e:  # noqa
        log("钉钉推送异常: %s" % e, "ERROR")
        return False


# ------------------------------------------------------------ Bark (iOS)
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
    detail = []
    any_ok = False
    imgs = t.get("img") or []
    has_wecom = bool((CONFIG.get("wecom_webhook") or "").strip())
    has_bark = bool((CONFIG.get("bark_key") or "").strip())
    has_dt = bool((CONFIG.get("dingtalk_webhook") or "").strip())

    if has_wecom:
        ok = push_wecom(build_markdown(t, cid_title))
        detail.append("企微=%s" % ("ok" if ok else "FAIL"))
        any_ok = any_ok or ok

    if has_dt:
        dt_title, dt_text = build_dingtalk(t, cid_title)
        ok = push_dingtalk(dt_title, dt_text)
        detail.append("钉钉=%s" % ("ok" if ok else "FAIL"))
        any_ok = any_ok or ok

    if has_bark:
        bt, bb, bu = build_bark_message(t)
        ok = push_bark(bt, bb, bu, icon=(imgs[0] if imgs else ""))
        detail.append("Bark=%s" % ("ok" if ok else "FAIL"))
        any_ok = any_ok or ok

    if has_wecom and imgs and CONFIG.get("push_images", DEFAULT_PUSH_IMAGES):
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
def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                st = json.load(f)
            st.setdefault("seen", [])
            st.setdefault("initialized", False)
            # pending: {tid: 已失败次数} —— 已发现但还没送达的帖子，下轮补发。
            # 旧状态文件没有这个键，setdefault 保证向前兼容。
            st.setdefault("pending", {})
            # sent_fp: {内容指纹: 推送时间戳} —— 跨 ID 防重复推送，与 seen 相互独立。
            # 旧状态文件没有这个键，setdefault 保证向前兼容。
            st.setdefault("sent_fp", {})
            return st
        except Exception as e:  # noqa
            # 以前这里静默 pass：状态损坏 → 返回空状态 → 下轮把整个列表当新基线
            # 且【不推送】，行为跟"首次运行"一模一样，日志里完全看不出异常。
            # 行为本身是安全的（不会重复轰炸），但必须留下痕迹。
            log("状态文件解析失败，已按空状态继续（本轮会重建基线、不推送）: %s" % e, "WARN")
    return {"seen": [], "initialized": False, "pending": {}, "sent_fp": {}}


def save_state(st):
    st["seen"] = st["seen"][-SEEN_KEEP:]

    # 指纹表：先按 TTL 淘汰过期项，再按数量留最新的一批，避免状态文件无限膨胀
    now = int(time.time())
    fp = st.get("sent_fp") or {}
    fp = {k: int(v) for k, v in fp.items() if now - int(v) < FP_TTL_SEC}
    if len(fp) > MAX_SENT_FP:
        fp = dict(sorted(fp.items(), key=lambda kv: kv[1])[-MAX_SENT_FP:])
    st["sent_fp"] = fp

    d = os.path.dirname(STATE_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, STATE_PATH)
    log("状态已保存（seen %d 条 / 内容指纹 %d 个 / 待补发 %d 条）"
        % (len(st["seen"]), len(fp), len(st.get("pending") or {})))


def archive_post(t):
    if not ARCHIVE_ENABLED:
        return
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    day = datetime.now(CST).strftime("%Y-%m-%d")
    with open(os.path.join(ARCHIVE_DIR, "%s.jsonl" % day), "a", encoding="utf-8") as f:
        f.write(json.dumps(t, ensure_ascii=False) + "\n")


# ------------------------------------------------------------ 主流程
def _deliver(state, tid, t, cid_title):
    """推送一条帖子并维护状态。返回 'ok' / 'dupe' / 'retry' / 'giveup'。

    ⭐ 只有推送成功（或重试到上限放弃）才写 seen；
      以前 seen 是无条件追加的，任何一次瞬时故障都会把帖子永久标记为"已处理"。

    ⭐ 推送前先查内容指纹：**同一条内容在 TTL 内只推一次**，
      与帖子 ID 无关。这样无论"别人重发同一件事"还是"状态丢失后同一 ID 被重推"，
      都不会再发第二遍。
    """
    key = str(tid)
    fp = content_fingerprint(t)

    if fp:
        last = state["sent_fp"].get(fp)
        if last and time.time() - last < FP_TTL_SEC:
            state["pending"].pop(key, None)
            state["seen"].append(key)
            log("内容重复，跳过 #%s（同内容已于 %.1f 小时前推送过）"
                % (key, (time.time() - last) / 3600.0), "WARN")
            return "dupe"

    if push_all(t, cid_title):
        state["pending"].pop(key, None)
        state["seen"].append(key)
        if fp:
            state["sent_fp"][fp] = int(time.time())
        return "ok"

    n = state["pending"].get(key, 0) + 1
    if n >= MAX_PUSH_RETRY:
        state["pending"].pop(key, None)
        state["seen"].append(key)
        log("推送失败 #%s，累计 %d 次，放弃重试" % (key, n), "ERROR")
        return "giveup"

    state["pending"][key] = n
    log("推送失败 #%s（第 %d 次，下轮补发）" % (key, n), "WARN")
    return "retry"


def _resend_pending(state, cid_title, dry):
    """补发上一轮未送达的帖子（按原顺序）。返回 (成功条数, 尝试条数)。"""
    if dry or not state.get("pending"):
        return 0, 0
    ok = 0
    attempted = 0
    log("上轮有 %d 条未送达，先补发" % len(state["pending"]))
    for key in list(state["pending"].keys()):
        try:
            t = fetch_thread(key)
        except Exception as e:  # noqa
            log("补发抓详情失败 %s: %s" % (key, e), "WARN")
            n = state["pending"].get(key, 0) + 1
            if n >= MAX_PUSH_RETRY:
                state["pending"].pop(key, None)
                state["seen"].append(key)
                log("补发 #%s 连续抓取失败，放弃" % key, "ERROR")
            else:
                state["pending"][key] = n
            save_state(state)
            continue
        r = _deliver(state, key, t, cid_title)
        if r == "ok":
            ok += 1
            attempted += 1
            log("补发成功 #%s" % key)
        elif r != "dupe":          # retry / giveup 属于"真的尝试过"；dupe 是主动跳过
            attempted += 1
        save_state(state)
        time.sleep(0.5)
    return ok, attempted


def run_once(dry=False):
    cfg = load_config()
    state = load_state()
    state.setdefault("pending", {})     # 兼容还没有 pending 字段的旧状态文件
    state.setdefault("sent_fp", {})     # 兼容还没有内容指纹表的旧状态文件

    # 只要任意一个渠道配置了就继续（注意：新增渠道时必须同步加进来，
    # 否则会出现「关了旧渠道 → 脚本以为没渠道 → 直接退出 → 一条不推」）
    _has_any = any((cfg.get(k) or "").strip() for k in
                   ("wecom_webhook", "dingtalk_webhook", "bark_key"))
    if not _has_any and not dry:
        log("没有配置任何推送渠道，退出", "ERROR")
        return EXIT_NO_CHANNEL

    data = fetch_list(cfg["cid"], 1)
    items = data.get("list") or []
    if not items:
        log("列表为空")
        return EXIT_OK
    cid_title = data.get("title") or ("线报 %s" % cfg["cid"])

    if not state.get("initialized"):
        state["seen"].extend(str(it.get("ID")) for it in items)
        state["initialized"] = True
        save_state(state)
        log("首次运行：记录 %d 条为基线，不推送" % len(items))
        if not cfg.get("push_on_first_run"):
            return EXIT_OK

    # ⚠️ 必须把 pending 里的也算成"已见过"：否则它们会被当成新帖再推一遍，
    #    结果同一轮里「补发一次 + 当新帖再推一次」= 重复推送。
    seen = set(state["seen"]) | set(state["pending"])
    new_items = [it for it in items if str(it.get("ID")) not in seen]

    ok = 0
    attempted = 0

    # 阶段 1：先把上一轮没送达的补出去（这部分不占本轮新帖配额）
    r_ok, r_attempted = _resend_pending(state, cid_title, dry)
    ok += r_ok
    attempted += r_attempted

    # 阶段 2：处理本轮新帖
    if new_items:
        new_items.reverse()             # 列表是「新→旧」，反转后按时间顺序推
        cap = cfg.get("max_posts_per_cycle", 12)
        if len(new_items) > cap:
            log("本轮新帖 %d 条，只处理最早 %d 条" % (len(new_items), cap), "WARN")
            new_items = new_items[:cap]

        log("发现 %d 条新帖" % len(new_items))
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
                state["seen"].append(str(tid))
            else:
                r = _deliver(state, tid, t, cid_title)
                if r == "ok":
                    ok += 1
                    attempted += 1
                    log("已推送 #%s %s" % (tid, clean_text(t.get("title"))[:40]))
                elif r != "dupe":
                    attempted += 1
            save_state(state)
            time.sleep(0.5)

    log("本轮完成，成功推送 %d 条" % ok)

    # 有推送动作但一条都没成功 → 判定为故障，让 run 变红（这样才会被注意到）
    if attempted and not ok:
        log("本轮 %d 条推送全部失败" % attempted, "ERROR")
        return EXIT_ALL_PUSH_FAILED
    return EXIT_OK


def main():
    args = sys.argv[1:]
    load_config()

    # --once：跑一轮就退出（Actions 每轮调一次，所以它就是默认行为）。
    # 以前这个参数没被识别，只是"碰巧"落在最后一个分支才生效 ——
    # 看起来有语义、实际没有，会误导后来维护的人。这里显式接受它。
    args = [a for a in args if a != "--once"]

    if "--test" in args:
        ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        ok = False
        if (CONFIG.get("wecom_webhook") or "").strip():
            if push_wecom("**线报屋监控 · 推送测试**\n\n"
                          "如果你看到这条消息，说明 GitHub Actions 通道已打通。\n\n"
                          "> 监控板块：%s\n> 时间：%s" % (CONFIG.get("cid"), ts)):
                ok = True
        if (CONFIG.get("dingtalk_webhook") or "").strip():
            if push_dingtalk("线报屋监控 · 推送测试",
                             "如果你看到这条消息，说明 GitHub Actions 钉钉通道已打通。\n\n"
                             "监控板块：%s\n时间：%s" % (CONFIG.get("cid"), ts)):
                ok = True
        if (CONFIG.get("bark_key") or "").strip():
            if push_bark("[线报屋] 推送测试",
                         "GitHub Actions 通道已打通。\n板块：%s\n时间：%s"
                         % (CONFIG.get("cid"), ts)):
                ok = True
        log("测试推送 %s" % ("成功" if ok else "失败"))
        return 0 if ok else 1

    if "--backfill" in args:
        i = args.index("--backfill")
        n = int(args[i + 1]) if len(args) > i + 1 else 3
        d = fetch_list(CONFIG["cid"], 1)
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

    # 返回 run_once 的退出码，让「没配渠道 / 全部推送失败」能把 run 标红
    return run_once(dry=("--dry" in args))


if __name__ == "__main__":
    sys.exit(main())
