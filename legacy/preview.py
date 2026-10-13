# -*- coding: utf-8 -*-
"""预览推送效果（不发送）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hxm5_monitor as M

M.load_config()
d = M.fetch_list(M.CONFIG["cid"], 1)
title = d.get("title")
print("板块:", title, "| 帖子数:", len(d.get("list") or []))
print("=" * 70)
for it in (d.get("list") or [])[:2]:
    t = M.fetch_thread(it["ID"])
    md = M.build_wecom_markdown(t, title)
    print(md)
    print("-" * 70)
