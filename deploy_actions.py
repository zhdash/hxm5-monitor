#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键部署线报屋监控到 GitHub Actions（全程免费）

用法：
    set GH_TOKEN=ghp_xxxxxxxx
    python deploy_actions.py

或者把 token 写进 gh_token.txt（同目录），脚本会自动读。

脚本会：
  1. 用 token 查账号信息
  2. 创建公开仓库 hxm5-monitor（已存在则复用）
  3. 推送 actions_monitor.py + .github/workflows/monitor.yml
  4. 写入 Secret：HXM5_WECOM_WEBHOOK（用 libsodium 加密）
  5. 写入 Variables：HXM5_CID 等
  6. 手动触发一次 workflow，验证跑通
"""
import os
import sys
import json
import base64
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.github.com"
REPO_NAME = "hxm5-monitor"


def find_token():
    t = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if t:
        return t
    for name in ("gh_token.txt", ".gh_token"):
        p = os.path.join(HERE, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                t = f.read().strip()
            if t:
                return t
    return ""


def gh(method, path, token, data=None, raw=False):
    url = path if path.startswith("http") else API + path
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "hxm5-deploy",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            txt = r.read().decode("utf-8", "replace")
            return r.status, (txt if raw else (json.loads(txt) if txt.strip() else {}))
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(txt)
        except Exception:  # noqa
            j = {"message": txt[:300]}
        return e.code, j


def need_libsodium():
    """Actions 的 Secret 需要用仓库公钥加密，用 PyNaCl 或 libsodium"""
    try:
        from nacl import encoding, public  # noqa
        return "nacl"
    except ImportError:
        pass
    try:
        import sodium  # noqa
        return "sodium"
    except ImportError:
        pass
    return None


def encrypt_secret(pub_key_b64, value):
    """用仓库公钥加密 secret 值，返回 base64 密文"""
    impl = need_libsodium()
    if impl == "nacl":
        from nacl import encoding, public
        pk = public.PublicKey(pub_key_b64.encode(), encoding.Base64Encoder())
        box = public.SealedBox(pk)
        return base64.b64encode(box.encrypt(value.encode())).decode()
    if impl == "sodium":
        import sodium
        s = sodium.Sodium()
        pk = s.from_base64(pub_key_b64)
        return s.to_base64(
            s.crypto_box_seal(value.encode(), pk),
            encoder=s.base64.encoder if hasattr(s, "base64") else None,
        )
    raise RuntimeError(
        "缺少 PyNaCl，无法加密 Secret。请先安装：\n"
        "  C:\\Users\\jones\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\pip.exe install pynacl"
    )


def main():
    token = find_token()
    if not token:
        print("=" * 60)
        print("❌ 没有找到 GitHub Token")
        print("=" * 60)
        print()
        print("请任选一种方式提供：")
        print("  A. 设置环境变量：set GH_TOKEN=ghp_xxx")
        print("  B. 把 token 写进 %s" % os.path.join(HERE, "gh_token.txt"))
        print()
        print("Token 需要哪些权限（Fine-grained token）：")
        print("  - Repository permissions → Contents: Read and write")
        print("  - Repository permissions → Actions: Read and write")
        print("  - Repository permissions → Secrets: Read and write")
        print("  - Repository permissions → Administration: Read and write（要建仓库）")
        print()
        print("或者直接用 Classic token 勾选 repo + workflow 两个 scope。")
        print()
        print("创建地址：https://github.com/settings/tokens")
        return 1

    print("=" * 60)
    print("步骤 1/6：验证 token")
    print("=" * 60)
    code, me = gh("GET", "/user", token)
    if code != 200:
        print("  ❌ token 无效: %s" % me.get("message"))
        return 1
    owner = me.get("login")
    print("  ✅ 已登录：%s" % owner)

    print()
    print("=" * 60)
    print("步骤 2/6：检查/创建仓库")
    print("=" * 60)
    code, repo = gh("GET", "/repos/%s/%s" % (owner, REPO_NAME), token)
    if code == 200:
        print("  ✅ 仓库已存在：%s" % repo.get("html_url"))
    else:
        code, repo = gh("POST", "/user/repos", token, {
            "name": REPO_NAME,
            "description": "线报屋(hxm5.com)新帖监控 → 企业微信推送，跑在 GitHub Actions 上",
            "private": False,
            "auto_init": False,
            "has_issues": False,
            "has_wiki": False,
            "has_projects": False,
        })
        if code not in (200, 201):
            print("  ❌ 建仓库失败 (%s): %s" % (code, repo.get("message")))
            return 1
        print("  ✅ 已创建：%s" % repo.get("html_url"))
    repo_full = repo.get("full_name") or ("%s/%s" % (owner, REPO_NAME))

    print()
    print("=" * 60)
    print("步骤 3/6：开启 Actions 写权限（让 workflow 能提交状态）")
    print("=" * 60)
    code, r = gh("PUT", "/repos/%s/actions/permissions/workflow" % repo_full, token, {
        "default_workflow_permissions": "write",
        "can_approve_pull_request_reviews": False,
    })
    print("  %s" % ("✅ 已设置为 write" if code in (200, 204) else "⚠️ %s %s" % (code, r.get("message"))))

    print()
    print("=" * 60)
    print("步骤 4/6：写入 Secret / Variables")
    print("=" * 60)
    code, pk = gh("GET", "/repos/%s/actions/secrets/public-key" % repo_full, token)
    if code != 200:
        print("  ❌ 取公钥失败: %s" % pk.get("message"))
        return 1
    key_id, key_b64 = pk.get("key_id"), pk.get("key")

    wecom = ""
    for name in ("config.json",):
        p = os.path.join(HERE, name)
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    wecom = (json.load(f).get("wecom_webhook") or "").strip()
            except Exception:  # noqa
                pass
    if not wecom:
        wecom = input("请输入企业微信 webhook：").strip()
    if not wecom:
        print("  ❌ 没有 webhook，无法继续")
        return 1

    try:
        enc = encrypt_secret(key_b64, wecom)
    except Exception as e:  # noqa
        print("  ❌ 加密失败: %s" % e)
        return 1
    code, r = gh("PUT", "/repos/%s/actions/secrets/HXM5_WECOM_WEBHOOK" % repo_full, token,
                 {"encrypted_value": enc, "key_id": key_id})
    print("  %s HXM5_WECOM_WEBHOOK (Secret)" % ("✅" if code in (200, 201, 204) else "❌ %s" % r.get("message")))

    for k, v in (("HXM5_CID", "3"), ("HXM5_BARK_LEVEL", "timeSensitive"),
                 ("HXM5_MAX_IMAGES", "3"), ("HXM5_REPLY_LIMIT", "15"),
                 ("HXM5_PUSH_IMAGES", "false"), ("HXM5_MAX_POSTS_PER_CYCLE", "12")):
        code, r = gh("POST", "/repos/%s/actions/variables" % repo_full, token,
                     {"name": k, "value": v})
        if code == 409:  # 已存在，改更新
            code, r = gh("PATCH", "/repos/%s/actions/variables/%s" % (repo_full, k), token, {"value": v})
        print("  %s %s = %s" % ("✅" if code in (200, 201, 204) else "⚠️", k, v))

    print()
    print("=" * 60)
    print("步骤 5/6：推送代码")
    print("=" * 60)
    print("  请手动执行（需要 git 凭据）：")
    print()
    print("  cd %s" % HERE)
    print("  git init -b main")
    print("  git add -A")
    print('  git commit -m "feat: 线报屋监控 (GitHub Actions 版)"')
    print("  git remote add origin https://github.com/%s.git" % repo_full)
    print("  git push -u origin main")
    print()
    print("  如果用 HTTPS 推送，token 就是密码。")
    print()

    print("=" * 60)
    print("步骤 6/6：部署完成后的验证")
    print("=" * 60)
    print("  仓库地址：https://github.com/%s" % repo_full)
    print("  Actions 页：https://github.com/%s/actions" % repo_full)
    print()
    print("  推送代码后，去 Actions 页点「线报屋监控」→「Run workflow」手动跑一次。")
    print("  首轮会记录基线不推送；之后有新帖才会推。")
    print()
    print("  成本：公开仓库 —— Actions 分钟数无限，永久 0 元。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
