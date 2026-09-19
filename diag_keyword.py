#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
diag_keyword.py —— 验证 11128 是不是"提示词里出现了 Claude Code / Anthropic 品牌"
==============================================================================
【2026-09-13 复盘】实测结果：10 个变体**全部 200**，Claude Code / Anthropic 品牌词
和 11128 无关。加上 diag_size.py（体积无关）、diag_maxtokens.py（max_tokens / tools /
system 均无关），可以定性为：11128 与请求体无关，是上游服务端**成串出现**的拦截窗口
（连续 3 次被拦、跨度 > 6s）→ 详见 README 排错表。本脚本留作复发时的复现工具。

本脚本把这句话切片测试，每条请求都只有几十字节 —— 是当时用来排除"内容触发"的那组实验。
"""
import json
import sys
import time

sys.path.insert(0, r"C:\Users\21022\Desktop\Mark\wb-model-proxy")
import wb_proxy as w  # noqa: E402

MODEL = "hy3"

CASES = [
    ("K0-control",        "system", "You are a helpful assistant."),
    ("K1-full-line",      "system", "You are Claude Code, Anthropic's official CLI for Claude."),
    ("K2-claude-code",    "system", "You are Claude Code."),
    ("K3-anthropic-cli",  "system", "Anthropic's official CLI for Claude."),
    ("K4-codebuddy",      "system", "You are CodeBuddy, an official CLI for coding."),
    ("K5-tool-guide",     "system", "# Tool use guidelines\nAlways prefer editing existing files."),
    ("K6-user-turn",      "user",   "You are Claude Code, Anthropic's official CLI for Claude."),
    ("K7-claude-code-zh", "system", "你是 Claude Code，Anthropic 官方的命令行工具。"),
    ("K8-lowercase",      "system", "you are claude code, anthropic's official cli for claude."),
    ("K9-split",          "system", "You are Claude" + " Code, Anthropic's official CLI for Claude."),
]


def probe(tag, where, text):
    cfg = w.load_config()
    up = w.Upstream(cfg, None, w.Credentials(cfg, w.CONFIG_PATH))
    body = {"model": MODEL, "stream": True, "max_tokens": 8,
            "messages": [{"role": "user", "content": "1"}]}
    if where == "system":
        body["system"] = text
    else:
        body["messages"] = [{"role": "user", "content": text}]
    size = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    try:
        conn, resp = up._open_once(body)
    except Exception as e:
        print("  %-16s 连不上: %s" % (tag, str(e)[:60]))
        return
    code = 200
    if resp.status != 200:
        raw = resp.read()
        code = None
        try:
            j = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(j, dict):
                code = j.get("code")
        except Exception:
            pass
    else:
        try:
            resp.read(64)
        except Exception:
            pass
    try:
        conn.close()
    except Exception:
        pass
    verdict = "过" if resp.status == 200 else ("*11128*" if code == 11128 else "其它(%s)" % code)
    print("  %-16s %-7s 字节%-5d %-8s %s" % (tag, where, size, verdict, text[:48]), flush=True)


def main():
    print("模型 = %s\n" % MODEL)
    for tag, where, text in CASES:
        probe(tag, where, text)
        time.sleep(1.0)


if __name__ == "__main__":
    main()
