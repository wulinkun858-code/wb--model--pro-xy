#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
diag_maxtokens.py —— 验证 11128 是不是被 max_tokens 触发
==========================================================
线索：所有"必过"的探针（diag_size / diag_keyword / 我的连发测试）都用了
max_tokens=8；而唯一"必挂"的 diag_payload D1/D2/D3 都用了 max_tokens=1024。

本脚本只改 max_tokens 一个变量（消息恒为 1 个字符），再做两组交叉验证：
  1. max_tokens 扫描：8 → 64 → 256 → 1024 → 4096
  2. 固定 max_tokens=1024，分别叠加 tools / 大 system，看是否还有第二变量

messages 恒定，所以真实输出 token 很少，额度消耗可忽略。
"""
import json
import sys
import time

sys.path.insert(0, r"C:\Users\21022\Desktop\Mark\wb-model-proxy")
import wb_proxy as w  # noqa: E402

MODEL = "hy3"

TOOLS = [
    {"type": "function", "function": {
        "name": "Read", "description": "Read a file.",
        "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}},
                       "required": ["file_path"]}}},
    {"type": "function", "function": {
        "name": "Bash", "description": "Run a command.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                       "required": ["command"]}}},
]

BIG_SYSTEM = ("You are a helpful coding assistant. Be concise.\n") * 200   # ~8KB


def probe(tag, **kw):
    cfg = w.load_config()
    up = w.Upstream(cfg, None, w.Credentials(cfg, w.CONFIG_PATH))
    body = {"model": MODEL, "stream": True,
            "messages": [{"role": "user", "content": "1"}]}
    body.update(kw)
    size = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    try:
        conn, resp = up._open_once(body)
    except Exception as e:
        print("  %-22s 连不上: %s" % (tag, str(e)[:70]), flush=True)
        return None
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
    verdict = "过" if resp.status == 200 else ("**11128 拦截**" if code == 11128 else "其它(%s)" % code)
    print("  %-22s max_tokens=%-6s body=%-7d %s"
          % (tag, body.get("max_tokens"), size, verdict), flush=True)
    return code


def main():
    _cfg = w.load_config()
    _up = w.Upstream(_cfg, None, w.Credentials(_cfg, w.CONFIG_PATH))
    print("模型 = %s   上游 = %s\n" % (MODEL, _up.endpoint()))

    print("[第一组] 只扫 max_tokens（消息恒为 \"1\"，无 tools、无大 system）")
    for mt in (8, 64, 256, 512, 1024, 2048, 4096):
        probe("MT=%d" % mt, max_tokens=mt)
        time.sleep(1.0)

    print("\n[第二组] 固定 max_tokens=1024，叠加别的变量")
    probe("1024+tools", max_tokens=1024, tools=TOOLS)
    time.sleep(1.0)
    probe("1024+bigsystem", max_tokens=1024, system=BIG_SYSTEM)
    time.sleep(1.0)
    probe("1024+tools+bigsys", max_tokens=1024, tools=TOOLS, system=BIG_SYSTEM)

    print("\n[第三组] 稳定性：MT=1024 连发 3 次")
    for i in range(3):
        probe("1024-run%d" % (i + 1), max_tokens=1024)
        time.sleep(1.0)


if __name__ == "__main__":
    main()
