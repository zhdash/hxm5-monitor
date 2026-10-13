# -*- coding: utf-8 -*-
"""
腾讯云 API v3 签名 + 调用（纯标准库，严格对齐官方文档）
https://cloud.tencent.com/document/api/213/30654
"""
import json
import time
import hmac
import hashlib
import urllib.request
import urllib.error


class TC3:
    def __init__(self, secret_id, secret_key, region,
                 endpoint="scf.tencentcloudapi.com", version="2018-04-16"):
        self.sid = secret_id
        self.skey = secret_key
        self.region = region
        self.endpoint = endpoint
        self.version = version
        self.service = endpoint.split(".")[0]

    def _sign(self, action, payload_str, ts):
        date = time.strftime("%Y-%m-%d", time.gmtime(ts))

        # --- 步骤 1：规范请求串 ---
        hashed_payload = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()
        canonical_headers = ("content-type:application/json; charset=utf-8\n"
                             "host:%s\n"
                             "x-tc-action:%s\n" % (self.endpoint, action.lower()))
        signed_headers = "content-type;host;x-tc-action"
        canonical = "POST\n/\n\n%s\n%s\n%s" % (
            canonical_headers, signed_headers, hashed_payload)

        # --- 步骤 2：待签名字符串 ---
        scope = "%s/%s/tc3_request" % (date, self.service)
        string_to_sign = "TC3-HMAC-SHA256\n%d\n%s\n%s" % (
            ts, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest())

        # --- 步骤 3：计算签名 ---
        def _hmac(key, msg):
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        secret_date = _hmac(("TC3" + self.skey).encode("utf-8"), date)
        secret_service = _hmac(secret_date, self.service)
        secret_signing = _hmac(secret_service, "tc3_request")
        signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"),
                             hashlib.sha256).hexdigest()

        # --- 步骤 4：拼 Authorization ---
        return ("TC3-HMAC-SHA256 Credential=%s/%s, "
                "SignedHeaders=%s, Signature=%s"
                % (self.sid, scope, signed_headers, signature))

    def call(self, action, params, retries=2):
        ts = int(time.time())
        payload_str = json.dumps(params, ensure_ascii=False)
        auth = self._sign(action, payload_str, ts)
        headers = {
            "Authorization": auth,
            "Content-Type": "application/json; charset=utf-8",
            "Host": self.endpoint,
            "X-TC-Action": action,
            "X-TC-Timestamp": str(ts),
            "X-TC-Version": self.version,
            "X-TC-Region": self.region,
        }
        last = None
        for i in range(retries):
            try:
                req = urllib.request.Request(
                    "https://" + self.endpoint, data=payload_str.encode("utf-8"),
                    headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=60) as r:
                    body = json.loads(r.read().decode("utf-8", "replace"))
                resp = body.get("Response", {})
                if "Error" in resp:
                    raise RuntimeError("%s: %s" % (resp["Error"].get("Code"),
                                                   resp["Error"].get("Message")))
                return resp
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")
                try:
                    j = json.loads(detail)
                    err = j.get("Response", {}).get("Error", {})
                    last = RuntimeError("%s: %s" % (err.get("Code"), err.get("Message")))
                except Exception:
                    last = RuntimeError("HTTP %s: %s" % (e.code, detail[:300]))
            except Exception as e:
                last = e
            if i < retries - 1:
                time.sleep(2)
        raise last
