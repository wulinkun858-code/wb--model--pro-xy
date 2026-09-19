#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke_live.py —— 对**已经跑起来**的 wb_proxy 打一轮真实请求（会真的消耗 WorkBuddy 额度）

跑法：
    python wb_proxy.py          # 另一个终端先起代理
    python smoke_live.py                       # 默认测前两个官方模型
    python smoke_live.py --model hy3            # 指定模型
    python smoke_live.py --only-openai         # 只测 OpenAI 侧
    python smoke_live.py --only-anthropic      # 只测 Anthropic 侧
"""

import argparse
import http.client
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def load_cfg(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def req(cfg, method, path, body=None, stream=False, timeout=180):
    conn = http.client.HTTPConnection("127.0.0.1", int(cfg["port"]), timeout=timeout)
    h = {"Content-Type": "application/json", "Authorization": "Bearer " + cfg["api_key"]}
    if stream:
        h["Accept"] = "text/event-stream"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    try:
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        if not stream:
            return r.status, r.read(), dict(r.getheaders()), None, None
        t0, buf, first = time.time(), b"", None
        while True:
            c = r.read(64)
            if not c:
                break
            if first is None:
                first = time.time() - t0
            buf += c
        return r.status, buf, dict(r.getheaders()), first, time.time() - t0
    finally:
        conn.close()


def case(cfg, model, path, stream, label):
    body = {"model": model, "max_tokens": 600}
    if path.endswith("messages"):
        body["messages"] = [{"role": "user", "content": "只回答两个字：收到"}]
    else:
        body["messages"] = [{"role": "user", "content": "只回答两个字：收到"}]
    body["stream"] = stream
    try:
        st, raw, hdr, first, total = req(cfg, "POST", path, body, stream)
    except Exception as e:
        print("  [失败] %-34s %s: %s" % (label, type(e).__name__, e))
        return False
    if st != 200:
        print("  [失败] %-34s HTTP %s  %s" % (label, st, raw[:200].decode("utf-8", "replace")))
        return False
    txt = raw.decode("utf-8", "replace")
    if not stream:
        j = json.loads(txt)
        if path.endswith("messages"):
            content = "".join(b.get("text", "") for b in j.get("content", [])
                              if b.get("type") == "text")
        else:
            content = (j.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        timing = ""
    else:
        content = ""
        for block in txt.split("\n\n"):
            ev, data = None, None
            for line in block.split("\n"):
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    p = line[5:].strip()
                    if p and p != "[DONE]":
                        try:
                            data = json.loads(p)
                        except Exception:
                            pass
            if not data:
                continue
            if path.endswith("messages"):
                if ev == "content_block_delta":
                    d = data.get("delta") or {}
                    if d.get("type") == "text_delta":
                        content += d.get("text") or ""
            else:
                content += ((data.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
        timing = "  首包 %.2fs / 总 %.2fs" % (first or -1, total)
    ok = bool(content.strip())
    print("  [%s] %-34s%s" % ("通过" if ok else "空回复", label, timing))
    print("         %s" % (content.strip()[:100].replace("\n", " ") or "(没拿到正文)"))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--model", help="只测这个模型")
    ap.add_argument("--only-openai", action="store_true")
    ap.add_argument("--only-anthropic", action="store_true")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    print("\n=== wb_proxy 真机冒烟 ===  端口 %s" % cfg["port"])

    try:
        st, raw, _, _, _ = req(cfg, "GET", "/health")
        health = json.loads(raw.decode())
    except Exception as e:
        print("连不上代理：%s\n先在另一个终端跑 python wb_proxy.py" % e)
        return 2
    print("官方模型 %d 个（已跳过自定义/外部 %s 个）"
          % (health.get("official_models", 0), health.get("skipped_custom_models", 0)))
    cred = health["credential"]
    print("凭据来源：%s" % (cred["source"] if cred["configured"] else "未配置（会返回 503）"))

    if args.model:
        targets = [args.model]
    else:
        st, raw, _, _, _ = req(cfg, "GET", "/v1/models")
        data = json.loads(raw.decode())["data"]
        ids = [d["id"] for d in data if d.get("x_official")]
        if not ids:
            print("没有读到官方模型，先 python wb_proxy.py --list 看看。")
            return 2
        targets = ids[:2]        # 默认测前两个，够证明链路通就行

    ok, total = 0, 0
    for m in targets:
        print("\n--- %s ---" % m)
        if not args.only_anthropic:
            total += 2
            ok += 1 if case(cfg, m, "/v1/chat/completions", False, "OpenAI 非流式") else 0
            ok += 1 if case(cfg, m, "/v1/chat/completions", True, "OpenAI 流式") else 0
        if not args.only_openai:
            total += 2
            ok += 1 if case(cfg, m, "/v1/messages", False, "Anthropic 非流式") else 0
            ok += 1 if case(cfg, m, "/v1/messages", True, "Anthropic 流式") else 0

    print("\n冒烟结果：%d/%d 通过" % (ok, total))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
