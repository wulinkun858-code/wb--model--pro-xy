#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
diag_payload.py —— 换不同"请求体形态"打上游，看 11128 到底跟什么有关
========================================================================
diag_11128.py 显示当时的并发/节律无关（18/18 全 200）。本脚本逐个变体地试：

【2026-09-13 复盘 · 旧结论已推翻】本脚本当时"8KB/34KB 必挂、小体积必过"的判断是**跨时间窗比较**
得出的假阳性 —— 小体积的数据来自更早那次运行，D1/D2/D3 恰好落在拦截窗口里。之后的
diag_size.py（32KB 全 'a' 也过）、diag_keyword.py（Claude Code 品牌词全过）、
diag_maxtokens.py（体积 / tools / system / max_tokens 全过）把体积、内容、tools、max_tokens
四种解释逐一否掉。**真正结论：11128 与请求体无关，是上游服务端"成串出现"的拦截窗口**
（连续 3 次被拦、跨度 > 6s）→ 详见 README 排错表。本脚本只留作复发时的复现工具。

  D1  Claude Code 形态     ~8KB system + 3 个 tools + 两轮历史（走 Anthropic 转换）
  D2  同上但去掉 tools
  D3  只放大 system        ~30KB system，无 tools
  D4  混入非官方字段       OpenAI 形态 body 里塞 metadata / stream_options /
                           parallel_tool_calls / user / store 等官方 CLI 不会发的键
  D5  带工具调用的历史      assistant.tool_calls + role=tool 的消息结构

哪个变体出现 11128，哪个就是元凶。
"""
import json
import sys

sys.path.insert(0, r"C:\Users\21022\Desktop\Mark\wb-model-proxy")
import wb_proxy as w  # noqa: E402

MODEL = "hy3"

BIG_SYSTEM = (
    "You are Claude Code, Anthropic's official CLI for Claude.\n"
    "You are an interactive CLI tool that helps users with software engineering tasks.\n"
) + ("# Tool use guidelines\nAlways prefer editing existing files. Never create files unless "
     "absolutely necessary. Be concise in your responses.\n") * 60

TOOLS = [
    {"name": "Read", "description": "Reads a file from the local filesystem.",
     "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}},
                      "required": ["file_path"]}},
    {"name": "Bash", "description": "Runs a bash command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},
    {"name": "Edit", "description": "Performs exact string replacement in a file.",
     "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"},
                                                       "old_string": {"type": "string"},
                                                       "new_string": {"type": "string"}},
                      "required": ["file_path", "old_string", "new_string"]}},
]


def post(tag, body, anthropic=True):
    cfg = w.load_config()
    up = w.Upstream(cfg, None, w.Credentials(cfg, w.CONFIG_PATH))
    up_body = w.anthropic_to_openai(body, MODEL) if anthropic else dict(body)
    up_body["model"] = MODEL
    up_body["stream"] = True
    size = len(json.dumps(up_body, ensure_ascii=False))
    try:
        conn, resp = up._open_once(up_body)
    except Exception as e:
        print("  %-6s 连不上: %s" % (tag, str(e)[:80]))
        return None
    if resp.status == 200:
        try:
            resp.read(64)
        except Exception:
            pass
        code, note = 200, "ok"
    else:
        raw = resp.read()
        code, note = None, raw[:200].decode("utf-8", "replace").replace("\n", " ")
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
    flag = "  <<< 11128 命中" if code == 11128 else ""
    print("  %-6s HTTP %-5s code=%-7s body=%-7d %s%s"
          % (tag, resp.status, code, size, note[:110], flag), flush=True)
    return resp.status


def main():
    print("模型 = %s\n" % MODEL)

    # D1 Claude Code 典型形态
    base = {
        "model": MODEL, "max_tokens": 1024, "stream": True,
        "system": [{"type": "text", "text": BIG_SYSTEM,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "你好"}],
        "tools": TOOLS,
        "tool_choice": {"type": "auto"},
        "metadata": {"user_id": "u"},
    }
    print("[D1] Claude Code 形态：大 system + 3 tools + tool_choice")
    post("D1", base)

    print("\n[D2] 同上，去掉 tools / tool_choice")
    d2 = {k: v for k, v in base.items() if k not in ("tools", "tool_choice")}
    post("D2", d2)

    print("\n[D3] 超大 system（~30KB），无 tools")
    post("D3", {k: v for k, v in base.items() if k not in ("tools", "tool_choice")}
         | {"system": BIG_SYSTEM * 4})

    print("\n[D4] OpenAI 形态 + 混入非官方字段")
    post("D4", {
        "model": MODEL, "stream": True, "max_tokens": 64,
        "messages": [{"role": "user", "content": "你好"}],
        "metadata": {"user_id": "u"},
        "stream_options": {"include_usage": True},
        "parallel_tool_calls": True,
        "store": False,
        "user": "test",
        "response_format": {"type": "text"},
        "logprobs": False,
    }, anthropic=False)

    print("\n[D5] 带 tool_calls / tool_result 的历史结构")
    post("D5", {
        "model": MODEL, "max_tokens": 256, "stream": True,
        "system": "You are Claude Code.",
        "messages": [
            {"role": "user", "content": "读一下 a.txt"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "好的"},
                {"type": "tool_use", "id": "toolu_01", "name": "Read",
                 "input": {"file_path": "a.txt"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01", "content": "hello"}]},
            {"role": "user", "content": "什么意思"},
        ],
        "tools": TOOLS,
    })


if __name__ == "__main__":
    main()
