# -*- coding: utf-8 -*-
"""
COS 状态存储（自包含，无第三方依赖）

为什么需要它：SCF 是无状态容器，/tmp 随实例回收而消失，
导致每轮都退化为「时间窗模式」，重复推送。把已推送的帖子 ID
存到对象存储 COS，才能做精确去重。

签名要点（踩过的坑）：
  - format_string = "{method}\\n{path}\\n{params}\\n{headers}\\n"
  - params / headers 内部用 **&** 连接（不是 ;），按 key 排序
  - header 名小写 + URL 编码（safe="-_.~"），header 值同样编码
    （斜杠会被编码为 %2F，不能整体 lower）
"""
import time
import hmac
import hashlib
import json
import urllib.request
import urllib.parse
import urllib.error


class CosState:
    def __init__(self, secret_id, secret_key, region, bucket, key="state/hxm5_state.json"):
        self.sid = secret_id
        self.skey = secret_key
        self.region = region
        self.bucket = bucket
        self.key = key.lstrip("/")
        self.host = "%s.cos.%s.myqcloud.com" % (bucket, region)

    # ------------------------------------------------ 签名
    def _sign(self, method, path, headers, params=None):
        now = int(time.time())
        sign_time = "%d;%d" % (now - 60, now + 3600)

        VALID = ("cache-control", "content-disposition", "content-encoding",
                 "content-type", "content-md5", "content-length", "expect",
                 "expires", "host", "if-match", "if-modified-since",
                 "if-none-match", "if-unmodified-since", "origin", "range",
                 "transfer-encoding", "pic-operations")
        picked = {}
        for k, v in headers.items():
            lk = k.lower()
            if lk == "authorization":
                continue
            if lk in VALID or lk.startswith("x-cos-") or lk.startswith("x-ci-"):
                picked[lk] = v

        enc_h = {urllib.parse.quote(k, safe="-_.~").lower():
                 urllib.parse.quote(str(v), safe="-_.~") for k, v in picked.items()}
        enc_p = {urllib.parse.quote(k, safe="-_.~").lower():
                 urllib.parse.quote(str(v), safe="-_.~") for k, v in (params or {}).items()}

        format_string = "%s\n%s\n%s\n%s\n" % (
            method.lower(), path,
            "&".join("%s=%s" % (k, v) for k, v in sorted(enc_p.items())),
            "&".join("%s=%s" % (k, v) for k, v in sorted(enc_h.items())),
        )

        sts = "sha1\n%s\n%s\n" % (sign_time,
                                  hashlib.sha1(format_string.encode("utf-8")).hexdigest())
        sign_key = hmac.new(self.skey.encode("utf-8"), sign_time.encode("utf-8"),
                            hashlib.sha1).hexdigest()
        sig = hmac.new(sign_key.encode("utf-8"), sts.encode("utf-8"),
                       hashlib.sha1).hexdigest()

        return ("q-sign-algorithm=sha1&q-ak=%s&q-sign-time=%s&q-key-time=%s"
                "&q-header-list=%s&q-url-param-list=%s&q-signature=%s"
                % (self.sid, sign_time, sign_time,
                   ";".join(sorted(enc_h.keys())),
                   ";".join(sorted(enc_p.keys())), sig))

    # ------------------------------------------------ 请求
    def _request(self, method, key, body=None, content_type=None):
        path = "/" + key.lstrip("/")
        headers = {"Host": self.host}
        if content_type:
            headers["Content-Type"] = content_type
        headers["Authorization"] = self._sign(method, path, headers)
        req = urllib.request.Request(
            "https://%s%s" % (self.host, path), data=body,
            headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    # ------------------------------------------------ 业务
    def load(self):
        """读取状态，返回 dict；不存在或出错返回 None"""
        code, body = self._request("GET", self.key)
        if code != 200:
            return None
        try:
            st = json.loads(body.decode("utf-8"))
            if isinstance(st, dict) and "seen" in st:
                return st
        except Exception:
            pass
        return None

    def save(self, st):
        """写入状态；成功返回 True"""
        st = dict(st)
        st["seen"] = list(st.get("seen") or [])[-2000:]
        st["ts"] = int(time.time())
        data = json.dumps(st, ensure_ascii=False).encode("utf-8")
        code, _ = self._request("PUT", self.key, body=data,
                                content_type="application/json")
        return code == 200
