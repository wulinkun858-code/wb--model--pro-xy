#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
diag_replay.py —— 用 11128 现场的真实请求体做复现 / 部件二分
==============================================================
现场文件：run\dumps\11128-*.json（代理被拦时自动落盘的真实请求体）

已知结论（2026-09-13）：
  asis      101KB / 29 tools            -> 11128
  no_tools   18KB / 0 tools             -> 11128   （不是 tools）
  no_sysmsg  94KB / 29 tools            -> 11128   （不是 messages 里那条 role=system）
  tiny      108B  / glm-5.3-flash       -> 200     （不是模型名）
所以触发点在系统提示 / 消息正文 / 那几个额外字段里，本脚本按部件组合逐个试。

用法：python diag_replay.py <variant>
"""
import io
import json
import os
import sys

sys.path.insert(0, r"C:\Users\21022\Desktop\Mark\wb-model-proxy")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import wb_proxy as w  # noqa: E402

D = os.path.join(r"C:\Users\21022\Desktop\Mark\wb-model-proxy", "run", "dumps")
VARIANT = sys.argv[1] if len(sys.argv) > 1 else "asis"
EXTRA_KEYS = ("metadata", "thinking", "output_config", "context_management")


def load_scene():
    cands = sorted([x for x in os.listdir(D) if x.startswith("11128-")])
    if not cands:
        print("run\\dumps 里没有 11128-*.json —— 先复现一次拦截")
        sys.exit(1)
    print("现场文件 = %s" % cands[-1])
    return json.load(open(os.path.join(D, cands[-1]), encoding="utf-8"))["body"]


def base(model):
    return {"model": model, "max_tokens": 64, "stream": True,
            "messages": [{"role": "user", "content": "1"}]}


def build(orig):
    model = orig.get("model")
    b = base(model)

    if VARIANT == "asis":                       # 原样（只有 max_tokens 压小）
        b = json.loads(json.dumps(orig))
        b["max_tokens"] = 64
    elif VARIANT == "no_tools":
        b = json.loads(json.dumps(orig)); b["max_tokens"] = 64; b.pop("tools", None)
    elif VARIANT == "no_sysmsg":
        b = json.loads(json.dumps(orig)); b["max_tokens"] = 64
        b["messages"] = [m for m in b["messages"] if m.get("role") != "system"]
    # ---- 按部件组合 ----
    elif VARIANT == "extra_only":               # 只带那 4 个额外字段
        for k in EXTRA_KEYS:
            if k in orig:
                b[k] = orig[k]
    elif VARIANT == "sysreal":                  # 只带真实 system
        b["system"] = orig.get("system")
    elif VARIANT == "msgreal":                  # 只带真实 messages
        b["messages"] = orig.get("messages")
    elif VARIANT == "sysreal_msgreal":          # system + messages，不带 tools / 额外字段
        b["system"] = orig.get("system")
        b["messages"] = orig.get("messages")
    elif VARIANT == "fill30k":                  # 体积对照
        b["system"] = "a" * 30000
    elif VARIANT == "fill100k":
        b["system"] = "a" * 100000
    elif VARIANT == "tools_only":               # 只带 29 个 tools
        b["tools"] = orig.get("tools")
    # ---- system 逐块（现场 system 有 3 块：billing 头 / Claude Code 自述 / 大段提示）----
    elif VARIANT.startswith("sysblk"):
        idx = int(VARIANT[6:])
        b["system"] = (orig.get("system") or [])[idx].get("text", "")
    # ---- 把第 2 块（大块）切成半 / 四分之一 ----
    elif VARIANT.startswith("sys2cut"):
        parts = int(VARIANT[7:])                # 切几段
        txt = (orig.get("system") or [])[2].get("text", "")
        step = max(1, len(txt) // parts)
        # 用 sys2partN 形式指定第几段
        b["system"] = txt[0:step]
    elif VARIANT.startswith("sys2part"):
        n = int(VARIANT[8:])
        txt = (orig.get("system") or [])[2].get("text", "")
        step = max(1, len(txt) // 6)
        b["system"] = txt[n * step:(n + 1) * step]
    elif VARIANT == "model_hy3":
        b = json.loads(json.dumps(orig)); b["max_tokens"] = 64; b["model"] = "hy3"
    else:
        print("未知变体：%s" % VARIANT)
        sys.exit(2)
    return b


def main():
    orig = load_scene()
    body = build(orig)
    model = body.get("model") or "hy3"
    cfg = w.load_config()
    up = w.Upstream(cfg, None, w.Credentials(cfg, w.CONFIG_PATH))
    up_body = w.anthropic_to_openai(body, model)
    up_body["model"] = model
    up_body["stream"] = True
    size = len(json.dumps(up_body, ensure_ascii=False).encode())
    print("变体 = %-16s 上游 body = %-7d tools=%-3d messages=%d"
          % (VARIANT, size, len(up_body.get("tools") or []), len(up_body.get("messages") or [])))
    try:
        conn, resp = up._open_once(up_body)
    except Exception as e:
        print("连不上: %s" % str(e)[:120])
        return
    if resp.status == 200:
        resp.read(200)
        print("HTTP 200  >>> 通过（未被拦）")
    else:
        raw = resp.read()
        code = None
        try:
            j = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(j, dict):
                code = j.get("code")
        except Exception:
            pass
        print("HTTP %s  >>> code=%s" % (resp.status, code))
    try:
        conn.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
