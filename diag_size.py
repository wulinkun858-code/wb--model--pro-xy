#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
diag_size.py —— 扫出 11128 的触发阈值
========================================
【2026-09-13 复盘】本脚本的结论就是"体积无关"：32KB 的 'aaa…' 照样 200。所以当时
diag_payload.py 里"8KB 必挂"的判断是**跨时间窗比较**造成的假阳性，不是体积阈值。
真正结论见 README 排错表：11128 与请求体无关，是上游服务端成串出现的拦截窗口。

  控制变量：同样的 messages、同样的 max_tokens、只是 system 填充长度不同。
"""
import json
import sys
import time

sys.path.insert(0, r"C:\Users\21022\Desktop\Mark\wb-model-proxy")
import wb_proxy as w  # noqa: E402

MODEL = "hy3"
SIZES = [500, 1000, 1500, 2000, 2500, 3000, 4000, 6000, 8000, 16000, 32000]


def probe(pad):
    cfg = w.load_config()
    up = w.Upstream(cfg, None, w.Credentials(cfg, w.CONFIG_PATH))
    body = {
        "model": MODEL, "stream": True, "max_tokens": 8,
        "messages": [{"role": "user", "content": "1"}],
        "system": "a" * pad,
    }
    size = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    try:
        conn, resp = up._open_once(body)
    except Exception as e:
        return (size, -1, None, str(e)[:60])
    if resp.status == 200:
        try:
            resp.read(64)
        except Exception:
            pass
        code = 200
    else:
        raw = resp.read()
        code = None
        try:
            j = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(j, dict):
                code = j.get("code")
        except Exception:
            pass
    try:
        conn.close()
    except Exception:
        pass
    return (size, resp.status, code, "")


def main():
    print("模型 = %s   扫描 system 填充长度\n" % MODEL)
    print("  %-9s %-6s %-8s %s" % ("body字节", "HTTP", "code", "结论"))
    print("  " + "-" * 52)
    hits, miss = [], []
    for pad in SIZES:
        size, status, code, err = probe(pad)
        verdict = "过" if status == 200 else ("**11128 拦截**" if code == 11128 else "其它错误")
        print("  %-9d %-6s %-8s %s %s" % (size, status, code, verdict, err), flush=True)
        (hits if status == 200 else miss).append((size, code))
        time.sleep(1.0)

    print("\n=== 结论 ===")
    if hits and miss:
        last_ok = max(s for s, c in hits)
        first_bad = min(s for s, c in miss)
        print("  通过的最大请求体 : %d 字节" % last_ok)
        print("  被拦的最小请求体 : %d 字节" % first_bad)
        print("  → 阈值落在这两者之间")
    elif not miss:
        print("  全部通过，说明单看体积还不足以触发（可能还需叠加别的条件）")
    else:
        print("  全部被拦 —— 说明当前这个渠道/凭据状态下属于「一律拦截」级别的问题")


if __name__ == "__main__":
    main()
