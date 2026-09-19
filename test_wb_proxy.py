#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_wb_proxy.py —— wb_proxy（只代理 WorkBuddy 官方模型）离线自测

不需要联网、不需要凭据：进程内起一个"假装成 WorkBuddy 后端"的 OpenAI 兼容桩上游，
把 wb_proxy 指向它，然后逐条断言：

      A 官方模型筛选  只收没有 url 的官方条目；带 url / custom- 前缀 / vendor=Custom 的跳过
      B 凭据分层      config → 环境变量 → settings.json → apiKeyHelper，谁先有用谁；脱敏与来源标注
      C 鉴权          客户端侧 错 key / 无 key 401，对 key 放行
      D OpenAI 侧     /v1/models、/chat/completions 非流式与流式
      E Anthropic 请求转换  system / tool_use / tool_result / tools / tool_choice
      F Anthropic 非流式后转换  text / thinking / tool_use + stop_reason 映射
      G Anthropic 流式转换  事件序列完整且顺序正确、[DONE] 不泄漏
      H 错误处理      上游 401/500 按各自协议转成错误体；缺凭据 503
      I count_tokens
      J 上游请求头    带上了 Authorization / X-API-Key / X-User-Id / 如实标识的 User-Agent（不伪装官方 CLI），且 model 是官方 id

跑法：python test_wb_proxy.py      退出码 0 = 全绿
"""

import http.client
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, "run", "_test")

PASS, FAIL = [], []
LAST_REQ = {}
AUTH = {}          # 登录流程桩的状态


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [通过] %s" % name)
    else:
        FAIL.append((name, detail))
        print("  [失败] %s   %s" % (name, detail))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def load_mod():
    spec = importlib.util.spec_from_file_location("wbproxy", os.path.join(HERE, "wb_proxy.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------------------
# 桩上游：假装成 WorkBuddy 后端
# --------------------------------------------------------------------------------------

def sse(obj):
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, status, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _port(self):
        return self.server.server_address[1]

    def do_GET(self):
        # 官方登录流程：轮询换 token（前两次故意返回空，模拟"还没登录完"）
        if self.path.startswith("/v2/plugin/auth/token?"):
            AUTH["poll"] = AUTH.get("poll", 0) + 1
            AUTH["poll_headers"] = {k.lower(): v for k, v in self.headers.items()}
            if AUTH["poll"] < 3:
                return self._json(200, {"code": 0, "msg": "OK", "data": None})
            return self._json(200, {"code": 0, "msg": "OK", "data": {
                "accessToken": "tok-1", "refreshToken": "rt-1", "expiresIn": 3600,
                "account": {"uid": "u-9", "nickname": "测试号"}}})
        if self.path == "/v1/models":
            return self._json(200, {"object": "list", "data": [
                {"id": "stub-live-1", "object": "model"}]})
        self._json(404, {"error": "no such path"})

    def do_POST(self):
        # 官方登录流程：申请登录态
        if self.path.startswith("/v2/plugin/auth/state"):
            AUTH["state"] = True
            AUTH["state_headers"] = {k.lower(): v for k, v in self.headers.items()}
            return self._json(200, {"code": 0, "msg": "OK", "data": {
                "state": "s-1",
                "authUrl": "http://127.0.0.1:%d/login?platform=workbuddy&state=s-1" % self._port()}})
        # 官方登录流程：续期
        if self.path.startswith("/v2/plugin/auth/token/refresh"):
            rt = self.headers.get("X-Refresh-Token")
            AUTH["refresh_seen"] = rt
            if rt == "rt-1":
                return self._json(200, {"code": 0, "msg": "OK", "data": {
                    "accessToken": "fresh-token", "refreshToken": "rt-2"}})
            return self._json(401, {"code": 401, "msg": "bad refresh token"})

        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}

        # 模拟上游偶发的安全策略拦截：flaky_left>0 时，对对话请求先返回一次 11128
        if self.path == "/v1/chat/completions" and AUTH.get("flaky_left", 0) > 0:
            AUTH["flaky_left"] -= 1
            return self._json(400, {"code": 11128,
                                    "msg": "Illegal API invocation from an unapproved channel"})

        LAST_REQ.clear()
        LAST_REQ.update({"path": self.path,
                         "headers": {k.lower(): v for k, v in self.headers.items()},
                         "body": body})

        model = body.get("model")
        # 模拟 access token 过期：只有续期后的 fresh-token 才放行
        auth_hdr = self.headers.get("Authorization") or ""
        if auth_hdr == "Bearer expired-token":
            return self._json(401, {"error": {"message": "token expired"}})
        if model == "boom401":
            return self._json(401, {"error": {"message": "凭据无效"}})
        if model == "boom500":
            return self._json(500, {"error": {"message": "上游炸了"}})

        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            frames = [
                {"id": "c1", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {"role": "assistant",
                                                    "reasoning_content": "我先想一下。"}}]},
                {"id": "c1", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {"content": "你好"}}]},
                {"id": "c1", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {"content": "，世界。"}}]},
                {"id": "c1", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {"tool_calls": [
                     {"index": 0, "id": "call_abc", "type": "function",
                      "function": {"name": "get_weather", "arguments": '{"city"'}}]}}]},
                {"id": "c1", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {"tool_calls": [
                     {"index": 0, "function": {"arguments": ': "北京"}'}}]}}]},
                {"id": "c1", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                 "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33}},
            ]
            for fr in frames:
                data = sse(fr)
                for part in (data[:9], data[9:]):     # 故意拆两半写，验证字节级处理
                    if part:
                        self.wfile.write(b"%x\r\n" % len(part) + part + b"\r\n")
                        try:
                            self.wfile.flush()
                        except Exception:
                            pass
            tail = b"data: [DONE]\n\n"
            self.wfile.write(b"%x\r\n" % len(tail) + tail + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return

        self._json(200, {
            "id": "chatcmpl-stub", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": "你好，世界。",
                "reasoning_content": "我先想一下。",
                "tool_calls": [{"id": "call_abc", "type": "function",
                                "function": {"name": "get_weather",
                                             "arguments": '{"city": "北京", "n": 2}'}}]}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
        })


def start_stub(port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), StubHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1},
                     daemon=True).start()
    return srv


# --------------------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------------------

def call(port, method, path, body=None, key=None, timeout=30):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    h = {"Content-Type": "application/json"}
    if key is not None:
        h["Authorization"] = "Bearer " + key
    data = json.dumps(body).encode("utf-8") if body is not None else None
    try:
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        raw = r.read()
        return r.status, raw, dict(r.getheaders())
    finally:
        conn.close()


def jcall(port, path, body, key):
    st, raw, hdr = call(port, "POST", path, body, key)
    try:
        return st, json.loads(raw.decode("utf-8")), hdr
    except Exception:
        return st, raw.decode("utf-8", "replace"), hdr


def stream(port, path, body, key, timeout=30):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    h = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if key:
        h["Authorization"] = "Bearer " + key
    try:
        conn.request("POST", path, body=json.dumps(body).encode("utf-8"), headers=h)
        r = conn.getresponse()
        buf = b""
        while True:
            c = r.read(64)
            if not c:
                break
            buf += c
        return r.status, dict(r.getheaders()), buf
    finally:
        conn.close()


def parse_sse(raw):
    out = []
    for block in raw.decode("utf-8", "replace").split("\n\n"):
        ev, data = None, None
        for line in block.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                p = line[5:].strip()
                if p:
                    try:
                        data = json.loads(p)
                    except Exception:
                        data = p
        if ev:
            out.append((ev, data))
    return out


# --------------------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------------------

GW_KEY = "sk-wb-testkey-0001"
AUTH_TOKEN = "test-auth-token-abcdefg"

ENV_KEYS = ("CODEBUDDY_AUTH_TOKEN", "CODEBUDDY_API_KEY", "OPENAI_API_KEY")


def main():
    print("\n=== wb-model-proxy 离线自测（v2：只代理官方模型）===\n")
    mod = load_mod()
    os.makedirs(TMP, exist_ok=True)

    stub_port = free_port()
    gw_port = free_port()
    stub_base = "http://127.0.0.1:%d" % stub_port
    srv_stub = start_stub(stub_port)

    # ---------- 造一份"产品配置"：混入官方与自定义条目 ----------
    product_cfg = os.path.join(TMP, "acc-product-config-v3.json")
    with open(product_cfg, "w", encoding="utf-8") as f:
        json.dump({"models": [
            # 官方（没有 url）→ 应该收
            {"id": "hy3", "name": "Hy3", "vendor": "j",
             "supportsToolCall": True, "supportsReasoning": True},
            {"id": "glm-5.3", "name": "GLM-5.3", "vendor": "e", "supportsToolCall": True},
            {"id": "boom401", "name": "会401", "vendor": "tencent"},
            {"id": "boom500", "name": "会500", "vendor": "tencent"},
            # 自定义（有 url）→ 应该跳过
            {"id": "my-kimi", "name": "自定义", "vendor": "Custom",
             "url": "https://example.com/v1", "apiKey": "sk-mine"},
            {"id": "custom-local:deepseek-x", "name": "自定义带前缀", "vendor": "Custom",
             "url": "https://example.com/v1", "apiKey": "sk-mine2"},
            # vendor=Custom 但没有 url → 也跳过（不是官方提供）
            {"id": "weird-one", "name": "怪东西", "vendor": "Custom"},
        ]}, f, ensure_ascii=False)

    settings_file = os.path.join(TMP, "settings.json")
    with open(settings_file, "w", encoding="utf-8") as f:
        json.dump({"env": {}}, f)

    mod.PRODUCT_CONFIG_CANDIDATES = [product_cfg]
    mod.PRODUCT_CONFIG_FILES = []
    mod.WB_SETTINGS_FILE = settings_file

    def make_cfg(token=AUTH_TOKEN, api_key="", helper=""):
        return {
            "host": "127.0.0.1", "port": gw_port, "api_key": GW_KEY,
            "allow_any_key": False, "models_cache_ttl": 1, "default_model": "hy3",
            # 自测里把 11128 的退避压短，否则 N 组要白等 20 多秒
            "transient_retry_waits": [1, 2, 3],
            "dump_on_11128": True,
            "dump_11128_dir": os.path.join(TMP, "dumps11128"),
            "upstream": {"endpoint": stub_base, "path": "/v1/chat/completions",
                         "auth_token": token, "api_key": api_key,
                         "api_key_helper": helper, "user_id": "u-123"},
        }

    saved_env = {k: os.environ.pop(k, None) for k in ENV_KEYS}

    try:
        # ---------------- A 官方模型筛选 ----------------
        print("-- A 官方模型筛选 --")
        cfg = make_cfg()
        cfg_path = os.path.join(TMP, "gw-config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        registry = mod.ModelRegistry(cfg)
        creds = mod.Credentials(cfg, cfg_path)
        upstream = mod.Upstream(cfg, registry, creds)
        ids = [m.id for m in registry.models]
        check("A1 只收没有 url 的官方条目", set(ids) == {"hy3", "glm-5.3", "boom401", "boom500"},
              str(ids))
        check("A2 带 url 的自定义模型被跳过", "my-kimi" not in ids, str(ids))
        check("A3 custom-local: 前缀条目被跳过",
              not any(i.startswith("custom-local:") for i in ids), str(ids))
        check("A4 vendor=Custom 的条目被跳过", "weird-one" not in ids, str(ids))
        check("A5 跳过数量统计正确", registry.skipped == 3, "skipped=%s" % registry.skipped)
        check("A6 模型条目带 x_official 标记与能力列表",
              registry.get("hy3").to_openai()["x_official"] is True
              and "reasoning" in registry.get("hy3").to_openai()["x_capabilities"],
              json.dumps(registry.get("hy3").to_openai(), ensure_ascii=False)[:160])
        check("A7 未知模型能宽容匹配", registry.get("GLM_5.3") is not None, "没匹配到")

        # ---------------- B 凭据分层 ----------------
        print("-- B 凭据分层 --")
        r = creds.resolve()
        check("B1 config.json 的 auth_token 优先级最高",
              r["source"].startswith("config.json") and r["auth_token"] == AUTH_TOKEN,
              r["source"])
        check("B2 ready() 判定为已配置", creds.ready()[0] is True, "未判定为就绪")

        os.environ["CODEBUDDY_AUTH_TOKEN"] = "env-token-xyz"
        os.environ["CODEBUDDY_API_KEY"] = "env-key-xyz"
        r = mod.Credentials(make_cfg(token="")).resolve()
        check("B3 config 空时回落到环境变量", r["source"] == "环境变量 CODEBUDDY_AUTH_TOKEN"
              and r["auth_token"] == "env-token-xyz", r["source"])
        del os.environ["CODEBUDDY_AUTH_TOKEN"]
        r = mod.Credentials(make_cfg(token="")).resolve()
        check("B4 再回落到环境变量 CODEBUDDY_API_KEY",
              r["source"] == "环境变量 CODEBUDDY_API_KEY" and r["api_key"] == "env-key-xyz",
              r["source"])
        del os.environ["CODEBUDDY_API_KEY"]

        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump({"env": {"CODEBUDDY_AUTH_TOKEN": "settings-token-xyz"}}, f)
        r = mod.Credentials(make_cfg(token="")).resolve()
        check("B5 再回落到 settings.json 的 env",
              "settings.json" in r["source"] and r["auth_token"] == "settings-token-xyz",
              r["source"])

        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump({"apiKeyHelper": "echo helper-token-xyz"}, f)
        r = mod.Credentials(make_cfg(token="")).resolve()
        check("B6 最后回落到 apiKeyHelper（执行脚本取 stdout）",
              "apiKeyHelper" in r["source"] and r["auth_token"] == "helper-token-xyz",
              "%s / %s" % (r["source"], r["auth_token"]))

        with open(settings_file, "w", encoding="utf-8") as f:      # 清空，测"什么都没有"
            json.dump({"env": {}}, f)
        r = mod.Credentials(make_cfg(token="")).resolve()
        check("B7 全都没有时判定为未配置", r["source"] == "" and not r["auth_token"], str(r))
        ci = mod.Credentials(make_cfg(token="a" * 40)).resolve()
        check("B8 脱敏函数不会暴露完整凭据", "…" in mod.mask(ci["auth_token"]),
              mod.mask(ci["auth_token"]))

        # 热加载：token 过期后用户 --set-token 覆盖，运行中的进程必须立刻用新的
        time.sleep(0.02)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(make_cfg(token="hot-reloaded-token-1"), f, ensure_ascii=False)
        r = creds.resolve()
        check("B9 config 改动后热加载生效（无需重启）",
              r["auth_token"] == "hot-reloaded-token-1", r["auth_token"])

        # --show-config 必须把 refresh_token 一起脱敏：它等价于一把能换出 access_token 的凭据
        sc_path = os.path.join(TMP, "show-config.json")
        sc_cfg = make_cfg(token="tok-should-be-masked-1")
        sc_cfg["upstream"]["refresh_token"] = "rt-should-be-masked-2"
        with open(sc_path, "w", encoding="utf-8") as f:
            json.dump(sc_cfg, f, ensure_ascii=False)
        pr = subprocess.run([sys.executable, os.path.join(HERE, "wb_proxy.py"),
                             "--config", sc_path, "--show-config"],
                            capture_output=True, text=True, encoding="utf-8")
        out = pr.stdout or ""
        check("B10 --show-config 同时脱敏 auth_token 与 refresh_token",
              "tok-should-be-masked-1" not in out and "rt-should-be-masked-2" not in out,
              out[:160])
        time.sleep(0.02)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(make_cfg(), f, ensure_ascii=False)   # 还原

        msg = [{"role": "user", "content": "你好"}]

        # ---------------- L 官方登录流程 ----------------
        print("-- L 官方登录流程（不需要会员 / 不需要 API Key）--")
        AUTH.clear()
        lc = mod.LoginClient(cfg)
        st_data = lc.auth_state()
        check("L1 申请登录态拿到 state + authUrl",
              st_data.get("state") == "s-1" and "state=s-1" in str(st_data.get("authUrl") or ""),
              json.dumps(st_data, ensure_ascii=False))
        check("L2 申请登录态带 4 个 X-No-* 头（完全不鉴权）",
              all(AUTH["state_headers"].get(h) == "true" for h in
                  ("x-no-authorization", "x-no-user-id",
                   "x-no-enterprise-id", "x-no-department-info")),
              json.dumps(AUTH.get("state_headers"), ensure_ascii=False)[:220])
        access, refresh_t, raw = lc.poll_token("s-1", timeout=20, interval=0.05)
        check("L3 轮询拿到 accessToken / refreshToken",
              access == "tok-1" and refresh_t == "rt-1", "%s / %s" % (access, refresh_t))
        check("L4 真的是轮询（登录未完成时返回空会继续等）", AUTH.get("poll", 0) >= 3,
              "poll=%s" % AUTH.get("poll"))
        check("L5 轮询同样带 X-No-* 头",
              AUTH["poll_headers"].get("x-no-authorization") == "true",
              str(AUTH["poll_headers"].get("x-no-authorization")))
        check("L6 find_tokens 能抠出嵌套的 token",
              mod.find_tokens({"data": {"auth": {"access_token": "A", "refresh_token": "R"}}})
              == {"access_token": "A", "refresh_token": "R"}, "没抠对")
        check("L7 find_user_id 能找到 uid", mod.find_user_id(raw) == "u-9", mod.find_user_id(raw))

        login_cfg_path = os.path.join(TMP, "login-config.json")
        login_cfg = make_cfg(token="")
        login_cfg["upstream"]["user_id"] = ""      # 留空，验证能自动从返回里取 uid
        with open(login_cfg_path, "w", encoding="utf-8") as f:
            json.dump(login_cfg, f, ensure_ascii=False)
        AUTH.clear()
        rc = mod.cmd_login(login_cfg, login_cfg_path, no_browser=True,
                           timeout=20, interval=0.05)
        saved = json.load(open(login_cfg_path, encoding="utf-8"))["upstream"]
        check("L8 cmd_login 端到端：写入 auth_token / refresh_token / user_id",
              rc == 0 and saved.get("auth_token") == "tok-1"
              and saved.get("refresh_token") == "rt-1" and saved.get("user_id") == "u-9",
              json.dumps({k: saved.get(k) for k in ("auth_token", "refresh_token", "user_id")},
                         ensure_ascii=False))

        # 登录页地址必须用 workbuddy.cn（App 实际用的那个），而不是 API 给的 codebuddy 跳转页
        lc3 = mod.LoginClient(cfg)
        u = lc3.auth_url("s-1", api_auth_url="http://api.example/login?state=s-1")
        check("L13 默认登录页指向 workbuddy.cn 且带 state/版本/会话id",
              "www.workbuddy.cn/login/" in u and "state=s-1" in u
              and "version=" in u and "loginSessionId=" in u, u[:150])
        cfg_lp = make_cfg()
        cfg_lp["upstream"]["login_page"] = "http://127.0.0.1:9/login/?state={state}&v={version}"
        u2 = mod.LoginClient(cfg_lp).auth_url("s-1")
        check("L14 配置了 login_page 模板时按配置拼",
              u2.startswith("http://127.0.0.1:9/login/?state=s-1&v="), u2[:120])

        lc2 = mod.LoginClient(cfg)
        a2, r2 = lc2.refresh("rt-1")
        check("L9 refresh 用 X-Refresh-Token 换到新 token",
              a2 == "fresh-token" and r2 == "rt-2" and AUTH.get("refresh_seen") == "rt-1",
              "%s / %s / seen=%s" % (a2, r2, AUTH.get("refresh_seen")))
        try:
            lc2.refresh("rt-bad")
            check("L10 坏 refresh_token 会报错", False, "居然没报错")
        except mod.UpstreamError:
            check("L10 坏 refresh_token 会报错", True)

        # access token 过期 → 自动续期 + 重试
        retry_cfg_path = os.path.join(TMP, "retry-config.json")
        retry_cfg = make_cfg(token="expired-token")
        retry_cfg["upstream"]["refresh_token"] = "rt-1"
        with open(retry_cfg_path, "w", encoding="utf-8") as f:
            json.dump(retry_cfg, f, ensure_ascii=False)
        reg3 = mod.ModelRegistry(retry_cfg)
        up3 = mod.Upstream(retry_cfg, reg3, mod.Credentials(retry_cfg, retry_cfg_path))
        port3 = free_port()
        srv3 = mod.Server(("127.0.0.1", port3), mod.Handler, retry_cfg, reg3, up3)
        threading.Thread(target=srv3.serve_forever, kwargs={"poll_interval": 0.1},
                         daemon=True).start()
        time.sleep(0.2)
        AUTH.clear()
        st, j, _ = jcall(port3, "/v1/chat/completions", {"model": "hy3", "messages": msg}, GW_KEY)
        check("L11 access token 过期时自动续期并重试成功",
              st == 200 and LAST_REQ["headers"].get("authorization") == "Bearer fresh-token",
              "%s / %s" % (st, LAST_REQ["headers"].get("authorization")))
        saved3 = json.load(open(retry_cfg_path, encoding="utf-8"))["upstream"]
        check("L12 续期后的新 token 写回 config.json（重启也不丢）",
              saved3.get("auth_token") == "fresh-token" and saved3.get("refresh_token") == "rt-2",
              json.dumps({k: saved3.get(k) for k in ("auth_token", "refresh_token")},
                         ensure_ascii=False))
        srv3.shutdown()
        srv3.server_close()

        # 残缺缓存回归：某个来源只有 3 条时，必须被其他来源补齐（实测踩过：官方模型掉到 3 个）
        partial_cfg = os.path.join(TMP, "acc-product-config-v2.json")
        with open(partial_cfg, "w", encoding="utf-8") as f:
            json.dump({"models": [
                {"id": "fast-model", "name": "快速", "vendor": "f"},
                {"id": "balanced-model", "name": "均衡", "vendor": "f"},
                {"id": "deep-model", "name": "极致", "vendor": "f"},
                {"id": "my-custom-x", "name": "自定义", "vendor": "Custom", "url": "https://e.com/v1"},
            ]}, f, ensure_ascii=False)
        mod.PRODUCT_CONFIG_CANDIDATES = [product_cfg, partial_cfg]
        ids_p = [x.id for x in mod.ModelRegistry(cfg).models]
        check("A8 残缺来源被其他来源补齐（仍含 hy3/glm-5.3）",
              "hy3" in ids_p and "glm-5.3" in ids_p, str(ids_p))
        check("A9 两来源合并后不重复", ids_p.count("hy3") == 1 and ids_p.count("fast-model") == 1,
              str(ids_p))
        mod.PRODUCT_CONFIG_CANDIDATES = [product_cfg]      # 还原
        registry.refresh(force=True)

        # ---------------- 起代理（带凭据） ----------------
        server = mod.Server(("127.0.0.1", gw_port), mod.Handler, cfg, registry, upstream)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                         daemon=True).start()
        time.sleep(0.3)

        # ---------------- C 鉴权 ----------------
        print("-- C 鉴权 --")
        st, _, _ = jcall(gw_port, "/v1/chat/completions", {"model": "hy3", "messages": msg}, "sk-wrong")
        check("C1 错 key 401", st == 401, str(st))
        st, _, _ = jcall(gw_port, "/v1/chat/completions", {"model": "hy3", "messages": msg}, None)
        check("C2 无 key 401", st == 401, str(st))
        st, _, _ = jcall(gw_port, "/v1/chat/completions", {"model": "hy3", "messages": msg}, GW_KEY)
        check("C3 正确 key 放行", st == 200, str(st))

        # ---------------- J 上游请求头 ----------------
        print("-- J 上游请求头 --")
        check("J1 带上 Authorization: Bearer <token>",
              LAST_REQ["headers"].get("authorization") == "Bearer " + AUTH_TOKEN,
              str(LAST_REQ["headers"].get("authorization")))
        check("J2 带上 X-User-Id", LAST_REQ["headers"].get("x-user-id") == "u-123",
              str(LAST_REQ["headers"].get("x-user-id")))
        check("J3 带上 x-cli / x-default-env",
              LAST_REQ["headers"].get("x-cli") == "1"
              and LAST_REQ["headers"].get("x-default-env") == "internal",
              str(LAST_REQ["headers"].get("x-cli")))
        check("J4 上游收到的 model 是官方 id", LAST_REQ["body"].get("model") == "hy3",
              str(LAST_REQ["body"].get("model")))
        check("J5 只配 auth_token 时不发 X-API-Key", "x-api-key" not in LAST_REQ["headers"],
              str(LAST_REQ["headers"].get("x-api-key")))
        check("J6 打到的路径就是配置的 path", LAST_REQ["path"] == "/v1/chat/completions",
              LAST_REQ["path"])
        # 代理不能匿名：http.client 默认不带 UA，所以必须显式发一个
        check("J8 带上如实标识的 User-Agent（不伪装官方 CLI）",
              LAST_REQ["headers"].get("user-agent") == mod.DEFAULT_USER_AGENT
              and "wb-model-proxy" in mod.DEFAULT_USER_AGENT,
              str(LAST_REQ["headers"].get("user-agent")))
        # user_agent 配置能覆盖
        time.sleep(0.02)
        cfg_ua = make_cfg()
        cfg_ua["user_agent"] = "my-own-agent/1.0"
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg_ua, f, ensure_ascii=False)
        jcall(gw_port, "/v1/chat/completions", {"model": "hy3", "messages": msg}, GW_KEY)
        check("J9 config.user_agent 能覆盖 UA",
              LAST_REQ["headers"].get("user-agent") == "my-own-agent/1.0",
              str(LAST_REQ["headers"].get("user-agent")))
        # extra_headers 优先级更高（用户想自己指定任何头都从这儿走）
        time.sleep(0.02)
        cfg_eh = make_cfg()
        cfg_eh["user_agent"] = "my-own-agent/1.0"
        cfg_eh["upstream"]["extra_headers"] = {"User-Agent": "extra-wins/9.9"}
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg_eh, f, ensure_ascii=False)
        jcall(gw_port, "/v1/chat/completions", {"model": "hy3", "messages": msg}, GW_KEY)
        check("J10 extra_headers 的 User-Agent 优先级最高",
              LAST_REQ["headers"].get("user-agent") == "extra-wins/9.9",
              str(LAST_REQ["headers"].get("user-agent")))
        time.sleep(0.02)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(make_cfg(), f, ensure_ascii=False)
        # --headers 自检命令不能泄漏凭据
        hd_cfg_path = os.path.join(TMP, "headers-config.json")
        hd_cfg = make_cfg(token="tok-should-not-leak-9")
        with open(hd_cfg_path, "w", encoding="utf-8") as f:
            json.dump(hd_cfg, f, ensure_ascii=False)
        pr = subprocess.run([sys.executable, os.path.join(HERE, "wb_proxy.py"),
                             "--config", hd_cfg_path, "--headers"],
                            capture_output=True, text=True, encoding="utf-8")
        hd_out = pr.stdout or ""
        check("J11 --headers 打印 UA 但把凭据脱敏",
              "wb-model-proxy/" in hd_out and "tok-should-not-leak-9" not in hd_out,
              hd_out[:200].replace("\n", " | "))
        # 运行中热更新 token（模拟 token 过期后 --set-token 覆盖）
        time.sleep(0.02)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(make_cfg(token="hot-token-2"), f, ensure_ascii=False)
        st, _, _ = jcall(gw_port, "/v1/chat/completions", {"model": "hy3", "messages": msg}, GW_KEY)
        check("J7 运行中新 token 立刻生效（不用重启代理）",
              LAST_REQ["headers"].get("authorization") == "Bearer hot-token-2",
              str(LAST_REQ["headers"].get("authorization")))
        time.sleep(0.02)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(make_cfg(), f, ensure_ascii=False)

        # ---------------- M 上游只支持流式：强制走流式 + 自己拼回来 ----------------
        print("-- M 上游流式适配（官方后端非流式会 400 code=11101）--")
        st, j, _ = jcall(gw_port, "/v1/chat/completions",
                         {"model": "hy3", "messages": msg, "stream": False}, GW_KEY)
        check("M1 客户端要非流式时，发给上游的仍是 stream=true",
              LAST_REQ["body"].get("stream") is True, str(LAST_REQ["body"].get("stream")))
        check("M2 非流式客户端拿到的是拼好的 chat.completion",
              st == 200 and j.get("object") == "chat.completion"
              and j["choices"][0]["message"]["content"] == "你好，世界。",
              json.dumps(j, ensure_ascii=False)[:180])
        check("M3 拼回来的响应带 usage",
              (j.get("usage") or {}).get("completion_tokens") == 22, str(j.get("usage")))
        check("M4 拼回来的 tool_calls 参数是完整 JSON",
              json.loads(j["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
              == {"city": "北京"},
              str(j["choices"][0]["message"].get("tool_calls"))[:200])

        class FakeResp(object):
            """每次只返回 1 字节，逼出 SSE 分片解析的各种边界。"""
            def __init__(self, data):
                self.data, self.pos = data, 0

            def read(self, n=256):
                if self.pos >= len(self.data):
                    return b""
                out = self.data[self.pos:self.pos + 1]
                self.pos += 1
                return out

        raw_sse = (
            b": keepalive\n\n"
            + sse({"index": 0, "choices": [{"index": 0, "delta": {"reasoning_content": "想"}}]})
            + sse({"index": 0, "choices": [{"index": 0, "delta": {"content": "你"}}]})
            + sse({"index": 0, "choices": [{"index": 0, "delta": {"content": "好"}}]})
            + sse({"index": 0, "choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "c9", "function": {"name": "f", "arguments": '{"a"'}}]}}]})
            + sse({"index": 0, "choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": ": 1}"}}]}}]})
            + sse({"index": 0, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                   "usage": {"prompt_tokens": 3, "completion_tokens": 4}})
            + b"data: [DONE]\n\n"
        )
        agg = mod.aggregate_openai_stream(FakeResp(raw_sse), "hy3")
        m = agg["choices"][0]["message"]
        check("M5 分片正文拼接正确（1 字节一片也不丢）", m["content"] == "你好", repr(m["content"]))
        check("M6 思考过程单独归到 reasoning_content",
              m.get("reasoning_content") == "想", repr(m.get("reasoning_content")))
        check("M7 tool_calls 参数跨片拼成合法 JSON",
              json.loads(m["tool_calls"][0]["function"]["arguments"]) == {"a": 1},
              repr(m["tool_calls"][0]["function"]["arguments"]))
        check("M8 finish_reason 与 usage 都带回",
              agg["choices"][0]["finish_reason"] == "tool_calls"
              and agg["usage"]["completion_tokens"] == 4, str(agg.get("usage")))
        check("M9 只有思考没有正文时 reasoning 仍保留",
              mod.aggregate_openai_stream(
                  FakeResp(sse({"choices": [{"index": 0, "delta": {"reasoning_content": "嗯"}}]})
                           + b"data: [DONE]\n\n"), "m")["choices"][0]["message"].get(
                      "reasoning_content") == "嗯", "没保留")
        got = list(mod.iter_sse_json(FakeResp(b"data: [DONE]\n\ndata: {\"a\":1}\n\n")))
        check("M10 iter_sse_json 跳过 [DONE]、后续块正常解析", got == [{"a": 1}], str(got))

        # ---------------- D OpenAI 侧 ----------------
        print("-- D OpenAI 协议 --")
        st, raw, _ = call(gw_port, "GET", "/v1/models", key=GW_KEY)
        body = json.loads(raw.decode())
        mids = [d["id"] for d in body["data"]]
        check("D1 /v1/models 只列官方模型",
              "hy3" in mids and "glm-5.3" in mids and "my-kimi" not in mids, str(mids))
        st, j, hdr = jcall(gw_port, "/v1/chat/completions", {"model": "hy3", "messages": msg}, GW_KEY)
        check("D2 非流式 200 且正文完整",
              st == 200 and j["choices"][0]["message"]["content"] == "你好，世界。",
              json.dumps(j, ensure_ascii=False)[:160])
        check("D3 响应头标出官方作用域与模型",
              hdr.get("X-Proxy-Scope") == "official" and hdr.get("X-Proxy-Model") == "hy3",
              "%s %s" % (hdr.get("X-Proxy-Scope"), hdr.get("X-Proxy-Model")))
        check("D4 reasoning_content 原样保留在 OpenAI 响应里",
              j["choices"][0]["message"].get("reasoning_content") == "我先想一下。",
              str(j["choices"][0]["message"].get("reasoning_content")))
        st, hdr, raw = stream(gw_port, "/v1/chat/completions",
                              {"model": "hy3", "messages": msg, "stream": True}, GW_KEY)
        check("D5 流式 SSE 帧齐全且以 [DONE] 收尾",
              st == 200 and raw.count(b"data:") >= 7 and raw.rstrip().endswith(b"[DONE]"),
              "data 行 %d" % raw.count(b"data:"))

        # ---------------- E Anthropic 请求转换 ----------------
        print("-- E Anthropic 请求转换 --")
        areq = {
            "model": "hy3", "max_tokens": 200, "temperature": 0.3,
            "system": [{"type": "text", "text": "你是助手"}],
            "tools": [{"name": "get_weather", "description": "查天气",
                       "input_schema": {"type": "object",
                                        "properties": {"city": {"type": "string"}}}}],
            "tool_choice": {"type": "any"},
            "messages": [
                {"role": "user", "content": "北京天气"},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "我查一下"},
                    {"type": "tool_use", "id": "tu1", "name": "get_weather",
                     "input": {"city": "北京"}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tu1",
                     "content": [{"type": "text", "text": "晴 25 度"}]}]},
            ],
        }
        o = mod.anthropic_to_openai(areq, "hy3")
        check("E1 system 块转成 system 消息",
              o["messages"][0] == {"role": "system", "content": "你是助手"}, str(o["messages"][0]))
        check("E2 assistant 的 tool_use 转成 tool_calls",
              o["messages"][2].get("tool_calls", [{}])[0]["function"]["name"] == "get_weather",
              json.dumps(o["messages"][2], ensure_ascii=False)[:180])
        check("E3 tool_result 转成 role=tool 且带 tool_call_id",
              o["messages"][3]["role"] == "tool" and o["messages"][3]["tool_call_id"] == "tu1",
              json.dumps(o["messages"][3], ensure_ascii=False))
        check("E4 input_schema 转成 parameters",
              o["tools"][0]["function"]["parameters"]["properties"]["city"]["type"] == "string",
              json.dumps(o["tools"][0], ensure_ascii=False)[:160])
        check("E5 tool_choice any → required", o["tool_choice"] == "required", str(o.get("tool_choice")))
        check("E6 max_tokens / temperature 透传",
              o["max_tokens"] == 200 and abs(o["temperature"] - 0.3) < 1e-9,
              json.dumps({k: o.get(k) for k in ("max_tokens", "temperature")}))

        # ---------------- F Anthropic 非流式后转换 ----------------
        print("-- F Anthropic 非流式响应 --")
        st, j, hdr = jcall(gw_port, "/v1/messages", areq, GW_KEY)
        check("F1 返回 standard message 结构",
              st == 200 and j.get("type") == "message" and j.get("role") == "assistant",
              json.dumps(j, ensure_ascii=False)[:180])
        types = [b["type"] for b in j.get("content", [])]
        check("F2 内容块含 thinking/text/tool_use", types == ["thinking", "text", "tool_use"], str(types))
        check("F3 stop_reason 映射为 tool_use", j.get("stop_reason") == "tool_use", str(j.get("stop_reason")))
        check("F4 tool_use 的 input 是对象", isinstance(j["content"][2]["input"], dict)
              and j["content"][2]["input"]["city"] == "北京", str(j["content"][2]))
        check("F5 usage 映射到 input/output_tokens",
              j["usage"]["input_tokens"] == 11 and j["usage"]["output_tokens"] == 22, str(j["usage"]))
        check("F6 上游收到的是 OpenAI 格式（system 变成 messages[0]）",
              LAST_REQ["body"]["messages"][0]["role"] == "system",
              json.dumps(LAST_REQ["body"]["messages"][:1], ensure_ascii=False))
        check("F7 上游收到 tools 的 parameters",
              LAST_REQ["body"]["tools"][0]["function"]["parameters"]["properties"]["city"],
              json.dumps(LAST_REQ["body"].get("tools"), ensure_ascii=False)[:160])

        # ---------------- G Anthropic 流式转换 ----------------
        print("-- G Anthropic 流式响应 --")
        st, hdr, raw = stream(gw_port, "/v1/messages", dict(areq, stream=True), GW_KEY)
        events = parse_sse(raw)
        names = [e for e, _ in events]
        check("G1 Content-Type 是 text/event-stream",
              "text/event-stream" in (hdr.get("Content-Type") or ""), str(hdr.get("Content-Type")))
        check("G2 以 message_start 开头、message_stop 结尾",
              names[:1] == ["message_start"] and names[-1:] == ["message_stop"], str(names))
        required = ["message_start", "content_block_start", "content_block_delta",
                    "content_block_stop", "message_delta", "message_stop"]
        check("G3 必需事件一个不少", all(r in names for r in required), str(sorted(set(names))))
        check("G4 start/stop 事件配对（3 个块）",
              names.count("content_block_start") == names.count("content_block_stop") == 3,
              "start=%d stop=%d" % (names.count("content_block_start"),
                                    names.count("content_block_stop")))
        deltas = [d for e, d in events if e == "content_block_delta"]
        dtypes = [d["delta"]["type"] for d in deltas]
        check("G5 三种 delta 都有",
              set(["thinking_delta", "text_delta", "input_json_delta"]).issubset(set(dtypes)), str(dtypes))
        text = "".join(d["delta"]["text"] for d in deltas if d["delta"]["type"] == "text_delta")
        check("G6 text_delta 拼回正文正确", text == "你好，世界。", repr(text))
        think = "".join(d["delta"]["thinking"] for d in deltas if d["delta"]["type"] == "thinking_delta")
        check("G7 thinking_delta 拼回思考过程正确", think == "我先想一下。", repr(think))
        pj = "".join(d["delta"]["partial_json"] for d in deltas if d["delta"]["type"] == "input_json_delta")
        check("G8 跨 chunk 的 partial_json 能拼成合法 JSON",
              json.loads(pj) == {"city": "北京"}, repr(pj))
        starts = [d["content_block"] for e, d in events if e == "content_block_start"]
        check("G9 tool_use 块带 id/name/空 input",
              any(b.get("type") == "tool_use" and b.get("id") == "call_abc"
                  and b.get("name") == "get_weather" and b.get("input") == {} for b in starts),
              json.dumps(starts, ensure_ascii=False)[:200])
        md = [d for e, d in events if e == "message_delta"]
        check("G10 message_delta 给出 stop_reason=tool_use",
              md and md[0]["delta"]["stop_reason"] == "tool_use",
              json.dumps(md, ensure_ascii=False)[:150])
        check("G11 message_start 在 message_delta 之前",
              names.index("message_start") < names.index("message_delta"), str(names))
        check("G12 不泄漏 OpenAI 的 [DONE]", b"[DONE]" not in raw, "泄漏了")

        # ---------------- H 错误处理 ----------------
        print("-- H 错误处理 --")
        st, _, _ = jcall(gw_port, "/v1/chat/completions", {"model": "boom401", "messages": msg}, GW_KEY)
        check("H1 OpenAI 侧上游 401 原样返回", st == 401, str(st))
        st, _, _ = jcall(gw_port, "/v1/chat/completions", {"model": "boom500", "messages": msg}, GW_KEY)
        check("H2 OpenAI 侧上游 500 原样返回", st == 500, str(st))
        st, j, _ = jcall(gw_port, "/v1/messages", {"model": "boom500", "max_tokens": 10,
                                                  "messages": msg}, GW_KEY)
        check("H3 Anthropic 侧错误体是 anthropic 的 error 结构",
              st == 500 and j.get("type") == "error" and "error" in j,
              json.dumps(j, ensure_ascii=False)[:200])
        st, j, _ = jcall(gw_port, "/v1/chat/completions", {"model": "不存在的模型", "messages": msg}, GW_KEY)
        check("H4 未知模型回落到默认模型（不报 404）", st == 200, str(st))
        st, _, _ = call(gw_port, "POST", "/no/such", {}, GW_KEY)
        check("H5 未知路径 404", st == 404, str(st))

        # 缺凭据时必须 503 且给指引
        cfg_nocred = make_cfg(token="")
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump({"env": {}}, f)
        reg2 = mod.ModelRegistry(cfg_nocred)
        up2 = mod.Upstream(cfg_nocred, reg2, mod.Credentials(cfg_nocred))
        port2 = free_port()
        srv2 = mod.Server(("127.0.0.1", port2), mod.Handler, cfg_nocred, reg2, up2)
        threading.Thread(target=srv2.serve_forever, kwargs={"poll_interval": 0.1},
                         daemon=True).start()
        time.sleep(0.2)
        st, j, _ = jcall(port2, "/v1/messages", {"model": "hy3", "max_tokens": 10, "messages": msg}, GW_KEY)
        blob = json.dumps(j, ensure_ascii=False)
        check("H6 缺凭据时 503 credential_missing", st == 503 and "credential_missing" in blob,
              "%s %s" % (st, blob[:160]))
        check("H7 缺凭据的提示指向官方 API Key 获取地址",
              "--set-api-key" in blob and "profile" in blob, blob[:240])
        srv2.shutdown()
        srv2.server_close()

        # ---------------- I count_tokens ----------------
        print("-- I count_tokens --")
        st, j, _ = jcall(gw_port, "/v1/messages/count_tokens",
                         {"model": "hy3", "messages": [{"role": "user", "content": "你好世界"}]}, GW_KEY)
        check("I1 返回正整数 input_tokens",
              st == 200 and isinstance(j.get("input_tokens"), int) and j["input_tokens"] > 0,
              json.dumps(j, ensure_ascii=False))

        # ---------------- N 上游偶发安全策略拦截（11128）自动重试 ----------------
        print("-- N 偶发 11128 自动重试 --")
        AUTH["flaky_left"] = 1
        t0 = time.time()
        st, j, _ = jcall(gw_port, "/v1/chat/completions", {"model": "hy3", "messages": msg}, GW_KEY)
        cost = time.time() - t0
        check("N1 偶发 11128 自动重试后成功", st == 200 and j["choices"][0]["message"]["content"] == "你好，世界。",
              "%s %s" % (st, json.dumps(j, ensure_ascii=False)[:120]))
        check("N2 重试有退避（耗时 >= 1 秒）", cost >= 1.0, "%.2fs" % cost)

        # 被拦时必须把现场请求体落盘 —— 触发条件复现不出来，靠这个留证据
        dumps = [os.path.join(TMP, "dumps11128", n)
                 for n in (os.listdir(os.path.join(TMP, "dumps11128"))
                           if os.path.isdir(os.path.join(TMP, "dumps11128")) else [])]
        ok_dump = False
        dump_detail = "目录里没有 11128-*.json"
        for dp in dumps:
            if os.path.basename(dp).startswith("11128-"):
                try:
                    saved = json.load(open(dp, encoding="utf-8"))
                except Exception as e:
                    dump_detail = "读不出来: %s" % e
                    continue
                ok_dump = (saved.get("body", {}).get("messages") == msg
                           and saved.get("model") == "hy3")
                dump_detail = "%s 有 %d 个文件" % (os.path.basename(dp), len(dumps))
                break
        check("N6 被 11128 拦时自动落盘现场请求体", ok_dump, dump_detail)

        # 12:54 那次线上事故的复现：连续 3 次都被拦。旧版（2s/4s 两段退避）在这里会直接失败，
        # 现在是「首次 + 3 次重试」，必须能扛过去。
        AUTH["flaky_left"] = 3
        t0 = time.time()
        st, j, _ = jcall(gw_port, "/v1/chat/completions", {"model": "hy3", "messages": msg}, GW_KEY)
        cost = time.time() - t0
        check("N3 连拦 3 次仍能靠退避重试成功",
              st == 200 and j["choices"][0]["message"]["content"] == "你好，世界。",
              "%s %s 耗时 %.2fs" % (st, json.dumps(j, ensure_ascii=False)[:100], cost))
        check("N4 连拦 3 次时累计退避 >= 3 秒", cost >= 3.0, "%.2fs" % cost)

        AUTH["flaky_left"] = 9
        st, j, _ = jcall(gw_port, "/v1/chat/completions", {"model": "hy3", "messages": msg}, GW_KEY)
        blob = json.dumps(j, ensure_ascii=False)
        check("N5 超出重试次数时把 11128 原样透传",
              st == 400 and "11128" in blob, "%s %s" % (st, blob[:160]))

        AUTH["flaky_left"] = 0

        # ---------------- O BASE_URL 多写 /v1 的兼容 ----------------
        print("-- O 路径归一化（BASE_URL 误带 /v1）--")
        st, raw, _ = call(gw_port, "GET", "/v1/v1/models", key=GW_KEY)
        check("O1 GET /v1/v1/models 归一化为 /v1/models", st == 200, str(st))
        st, j, _ = jcall(gw_port, "/v1/v1/messages", {"model": "hy3", "messages": msg}, GW_KEY)
        check("O2 POST /v1/v1/messages 归一化为 /v1/messages", st == 200, str(st))
        st, _, _ = call(gw_port, "GET", "/v1/models", key=GW_KEY)
        check("O3 正常写法不受影响", st == 200, str(st))

        # ---------------- health ----------------
        print("-- health --")
        st, raw, _ = call(gw_port, "GET", "/health")
        j = json.loads(raw.decode())
        check("health 免鉴权可读并报告官方模型数与来源",
              st == 200 and j.get("official_models") == 4 and j["credential"]["configured"],
              json.dumps({k: j.get(k) for k in ("official_models", "skipped_custom_models")}))
        check("health 不泄漏完整凭据", AUTH_TOKEN not in raw.decode(), "凭据泄漏")

    finally:
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        srv_stub.shutdown()
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v

    print("\n" + "=" * 64)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for name, detail in FAIL:
        print("  失败: %s   %s" % (name, detail))
    print("=" * 64 + "\n")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
