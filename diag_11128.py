#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
diag_11128.py —— 定位 code=11128「unapproved channel」的触发条件
==================================================================
思路：直接用代理同一套请求头/凭据打上游，控制变量做三组实验：

  A 单发（对照组）        连发 2 次，每次之间隔 3s
  B 并发 6 路             同一瞬间打 6 个请求
  C 快速连发 10 次        无间隔串行，模拟 Claude Code 的突发节律

如果 B/C 出现 11128 而 A 全绿 -> 结论是「请求节律/并发」触发风控；
如果 A 也挂 -> 说明是渠道指纹层面的长期判定，跟节律无关。

只发极少 token（max_tokens=8），确认完就退出。
"""
import json
import sys
import threading
import time
from collections import Counter

sys.path.insert(0, r"C:\Users\21022\Desktop\Mark\wb-model-proxy")
import wb_proxy as w  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "hy3"
RESULTS = []
LOCK = threading.Lock()


def one(tag):
    """发一次请求，返回 (tag, http_status, biz_code, 摘要)"""
    cfg = w.load_config()
    creds = w.Credentials(cfg, w.CONFIG_PATH)
    up = w.Upstream(cfg, None, creds)
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "1"}],
        "max_tokens": 8,
        "stream": True,
    }
    t0 = time.time()
    try:
        conn, resp = up._open_once(body)          # 绕过信号量：并发由本脚本自己控制
    except Exception as e:
        return (tag, -1, None, "连不上: %s" % str(e)[:80], 0.0)
    dt = time.time() - t0
    if resp.status == 200:
        try:
            resp.read(64)
        except Exception:
            pass
        summary = "ok"
        biz = None
    else:
        raw = resp.read()
        biz, summary = None, raw[:160].decode("utf-8", "replace").replace("\n", " ")
        try:
            j = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(j, dict):
                biz = j.get("code")
        except Exception:
            pass
    try:
        conn.close()
    except Exception:
        pass
    return (tag, resp.status, biz, summary, dt)


def run(tag):
    r = one(tag)
    with LOCK:
        RESULTS.append(r)
    print("  %-14s HTTP %-6s code=%-7s %5.2fs  %s"
          % (r[0], r[1], r[2], r[4], r[3][:90]), flush=True)


def main():
    _cfg = w.load_config()
    _up = w.Upstream(_cfg, None, w.Credentials(_cfg, w.CONFIG_PATH))
    print("模型 = %s   上游 = %s\n" % (MODEL, _up.endpoint()))

    print("[A] 对照组：单发 2 次，间隔 3s")
    run("A-single-1")
    time.sleep(3)
    run("A-single-2")

    print("\n[B] 并发 6 路（同一瞬间）")
    ths = [threading.Thread(target=run, args=("B-par-%d" % i,)) for i in range(6)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

    print("\n[C] 快速连发 10 次（不间隔）")
    for i in range(10):
        run("C-rush-%d" % (i + 1))

    print("\n=== 汇总 ===")
    cnt = Counter()
    for tag, status, biz, summary, dt in RESULTS:
        key = "200" if status == 200 else "HTTP %s code=%s" % (status, biz)
        cnt[key] += 1
    for k, v in cnt.most_common():
        print("  %-26s %d 次" % (k, v))
    blocked = [r for r in RESULTS if r[2] == 11128]
    print("\n  11128 次数: %d / %d" % (len(blocked), len(RESULTS)))
    if blocked:
        phases = Counter(r[0].split("-")[0] for r in blocked)
        print("  分布: " + ", ".join("%s=%d" % (k, v) for k, v in sorted(phases.items())))


if __name__ == "__main__":
    main()
