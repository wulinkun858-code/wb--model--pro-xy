#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
wb_proxy.py —— 把 **WorkBuddy 官方提供的模型** 暴露成本地 OpenAI / Anthropic 双协议接口
================================================================================

用途：让 Claude Code、Codex CLI、以及任何支持 OpenAI 或 Anthropic 协议的客户端，
      直接用上 WorkBuddy 官方那批模型（hy3 / glm-5.3 / kimi-k2.7 / deepseek-v4-pro / minimax-m3 …）。

**只代理官方模型。** 你自己在 WorkBuddy 里加的自定义模型（带 url + apiKey 的那种）
不归本脚本管 —— 那种你手里已经有 key 了，自己直连就行，中间再套一层纯属多余。

------------------------------------------------------------------------------
模型从哪来（自动发现，不用手抄）
------------------------------------------------------------------------------
读取 WorkBuddy 的模型注册表，只保留"官方提供"的条目（没有自带 url 的）：

  1. %ACC_PRODUCT_CONFIG_PATH%  或  ~/.workbuddy/cache/acc-product-config-v3.json  →  models[]
  2. <WorkBuddy>\resources\app.asar.unpacked\cli\product.json                      →  兜底

带 `url` / `apiKey` 的条目（=你的自定义模型）、`custom-local:` / `custom:` 前缀的条目、
以及 vendor 为 Custom / Enterprise 的条目都会被跳过。
跑 `python wb_proxy.py --list` 看实际发现了什么、跳过了多少。

------------------------------------------------------------------------------
官方模型的上游（逆向得到的结论，2026-09 实测）
------------------------------------------------------------------------------
    POST https://copilot.tencent.com/v2/chat/completions
    Authorization: Bearer <token>          ← 主鉴权
    X-API-Key: <api_key>                   ← API Key 方式（与上面可同时带）
    X-User-Id / X-WorkBuddy-Session-Id / x-cli: 1 / x-default-env: internal

请求体就是标准 OpenAI 格式（model / messages / stream / tools …）。所以本脚本要做的就是
"带上正确凭据 + 把 Anthropic 协议双向转换"，不需要做任何模型侧的适配。

------------------------------------------------------------------------------
凭据从哪来（推荐走官方 API Key；按优先级六层，全都不用改代码）
------------------------------------------------------------------------------
  ★ 最省事：去 https://copilot.tencent.com/profile/ 拿一个官方 API Key，然后
       python wb_proxy.py --set-api-key "<那个 key>"

  查找顺序（与官方 CLI 的优先级一致：AUTH_TOKEN > apiKeyHelper > API_KEY）：
  1) config.json 的 upstream.auth_token     ← python wb_proxy.py --set-token "..."（Bearer token）
  2) config.json 的 upstream.api_key        ← python wb_proxy.py --set-api-key "..."（官方 API Key）
  3) 环境变量 CODEBUDDY_AUTH_TOKEN / CODEBUDDY_API_KEY   （和 WorkBuddy CLI 同名，可直接复用。
     中国版记得同时设 CODEBUDDY_INTERNET_ENVIRONMENT=internal，官方文档说这是最常漏的一项）
  4) ~/.workbuddy/settings.json 的 env.CODEBUDDY_AUTH_TOKEN / env.CODEBUDDY_API_KEY
  5) ~/.workbuddy/settings.json 的 apiKeyHelper（一个能打印出 key 的脚本，stdout 即凭据）
  6) config.json 的 upstream.api_key_helper（同上，但路径写在本脚本自己的配置里）

`python wb_proxy.py --where` 可以看到当前凭据到底来自哪一层、值是什么（脱敏）。

> 官方 API Key 获取地址：中国版 https://copilot.tencent.com/profile/ ；
> 海外版 https://www.codebuddy.ai/profile/keys ；
> iOA 版 https://tencent.sso.copilot.tencent.com/profile/keys
> （见官方文档"身份和访问管理 → 个人用户：获取 API Key"）
>
> 如果你想用 CODEBUDDY_AUTH_TOKEN 那条路：登录网页端 F12 → Network 里随便抓一个请求，
> 复制 Request Headers 里 Authorization 的值（Bearer 后面那串）。token 会过期，
> 过期后 --check 会直接报 401/403。

------------------------------------------------------------------------------
对外接口
------------------------------------------------------------------------------
  GET  /health                       不带鉴权，看整体状态、凭据来源、模型数
  GET  /v1/models                    OpenAI 格式模型列表（只有官方模型）
  POST /v1/chat/completions          OpenAI 协议（流式/非流式，支持 tools）
  POST /v1/completions               同上（兼容老客户端）
  POST /v1/messages                  Anthropic 协议（Claude Code 走这个）
  POST /v1/messages/count_tokens     粗估 token

------------------------------------------------------------------------------
常用命令
------------------------------------------------------------------------------
  python wb_proxy.py                      # 启动（默认 127.0.0.1:8788）
  python wb_proxy.py --list               # 列出官方模型 + 跳过统计
  python wb_proxy.py --where              # 显示凭据来自哪一层
  python wb_proxy.py --check              # 体检：拿一个官方模型真发一次请求
  python wb_proxy.py --set-token XXX      # 保存凭据（Bearer token）
  python wb_proxy.py --set-api-key XXX    # 保存 API Key
  python wb_proxy.py --set-helper script  # 用脚本动态产出 key
  python wb_proxy.py --port 9000
  python wb_proxy.py --new-key
"""

import argparse
import copy
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------------------
# 常量与路径
# --------------------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

WB_CONFIG_DIR = os.environ.get("CODEBUDDY_CONFIG_DIR") or os.environ.get("WORKBUDDY_CONFIG_DIR") \
    or os.path.join(os.path.expanduser("~"), ".workbuddy")
WB_SETTINGS_FILE = os.path.join(WB_CONFIG_DIR, "settings.json")

# 官方模型清单的来源（按优先级）
PRODUCT_CONFIG_CANDIDATES = [
    os.environ.get("ACC_PRODUCT_CONFIG_PATH") or "",
    os.path.join(WB_CONFIG_DIR, "cache", "acc-product-config-v3.json"),
    os.path.join(WB_CONFIG_DIR, "cache", "acc-product-config-v2.json"),
]
PRODUCT_CONFIG_FILES = [
    r"D:\WorkBuddy\resources\app.asar.unpacked\cli\product.json",
    r"D:\WorkBuddy\resources\app.asar.unpacked\cli\product.internal.json",
]

DEFAULT_WB_ENDPOINT = "https://copilot.tencent.com"

# 官方获取 API Key 的地址（WorkBuddy 官方文档：身份和访问管理 → 个人用户获取 API Key）
APIKEY_PAGES = {
    "internal": "https://copilot.tencent.com/profile/",
    "public": "https://www.codebuddy.ai/profile/keys",
    "ioa": "https://tencent.sso.copilot.tencent.com/profile/keys",
}

# 登录页模板。**必须用 workbuddy.cn 这个域** —— 桌面端 App 实际跳的就是它
# （copilot.tencent.com/login 会 302 到 www.codebuddy.cn/login，那是 CodeBuddy 的登录页，
#  账号体系和 WorkBuddy 不是一套，登了也绑不到 state 上）。
# state 由 /v2/plugin/auth/state 发放，loginSessionId 是客户端自己生成的关联 id。
DEFAULT_LOGIN_PAGE = ("https://www.workbuddy.cn/login/?platform=workbuddy"
                      "&state={state}&version={version}&loginSessionId={loginSessionId}")

# WorkBuddy 内部给"自定义模型"加的 id 前缀 —— 带这些前缀的都不是官方模型
CUSTOM_PREFIXES = ("custom-local:", "custom:")

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8788,
    "api_key": "",
    "allow_any_key": False,
    "upstream": {
        "endpoint": DEFAULT_WB_ENDPOINT,
        "path": "/v2/chat/completions",
        "auth_token": "",
        "refresh_token": "",
        "api_key": "",
        "api_key_helper": "",
        "user_id": "",
        "session_id": "",
        "login_page": "",
        "extra_headers": {},
    },
    "default_model": "",
    "max_upstream_concurrency": 2,   # 同时打到上游的最大请求数。Claude Code 会并行发主请求+后台
                                     # 请求，并发太高会被上游重置连接/风控，串行一点更稳
    "dump_dir": "",                  # 开启后把每个进来的请求体原样落盘到这个目录，方便抓包排查
    "connect_timeout": 20,
    "read_timeout": 600,
    "models_cache_ttl": 60,
    # 11128「Illegal API invocation from an unapproved channel」的退避重试间隔（秒）。
    # 实测这个拦截是**成串**出现的（同一时刻连续 3 次被拦、跨度 > 6s；2026-09-13 又一次
    # 连 4 次全被拦、跨度 > 23s），窗口过去立刻恢复。所以退避要拉长到分钟级。
    # 嫌拖慢对话就调小；想更耐拦就加长或加一项。
    "transient_retry_waits": [2, 6, 15, 30, 60],
    # 一旦被 11128 拦，自动把这次请求体落盘到 run\dumps\（默认开）。
    # 触发条件至今没能稳定复现（体积/内容/tools/max_tokens/并发/连续 60 次量全都排除过），
    # 所以让它自己留证据：下次再被拦，run\dumps\11128-*.json 就是现场。
    "dump_on_11128": True,
    "dump_11128_dir": "",            # 11128 现场落盘目录；留空 = <项目>\run\dumps
    # 发给上游的 User-Agent。留空 = DEFAULT_USER_AGENT（如实说明"这是本地代理"）。
    # 之前代理**一个 UA 都不带**（http.client 不会自动加），正常 HTTP 客户端不该匿名，
    # 所以补上。想换成自己的写法就改这里，或写进 upstream.extra_headers（后者优先级更高）。
    "user_agent": "",
}

DEFAULT_TRANSIENT_RETRY_WAITS = (2.0, 6.0, 15.0, 30.0, 60.0)

# 如实标识自己，不要用一个假的官方 UA —— 理由见 README「关于请求指纹」。
DEFAULT_USER_AGENT = "wb-model-proxy/2.0 (+local)"


def transient_retry_waits(cfg):
    """11128 的退避间隔（秒）；列表长度 = 最多重试次数（首次尝试不计）。

    config.json -> transient_retry_waits 可覆盖；配了非法值就退回默认三段。
    """
    raw = (cfg or {}).get("transient_retry_waits")
    if raw is None:
        raw = DEFAULT_CONFIG["transient_retry_waits"]
    waits = []
    if isinstance(raw, (list, tuple)):
        for v in raw:
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f >= 0:
                waits.append(f)
    return waits or list(DEFAULT_TRANSIENT_RETRY_WAITS)

# --------------------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()


def _line_buffer():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(line_buffering=True)
        except Exception:
            pass


def log(msg):
    with _LOG_LOCK:
        try:
            print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)
        except Exception:
            pass


def gen_key():
    return "sk-wb-" + secrets.token_hex(20)


def deep_merge(base, override):
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for k, v in override.items():
            out[k] = deep_merge(base[k], v) if k in base else v
        return out
    return override


def load_config(path=CONFIG_PATH, create=True):
    """加载配置并保证每层默认值齐全；本地 api_key 为空时生成并持久化。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = deep_merge(cfg, json.load(f))
        except Exception as e:
            log("config.json 解析失败，用默认值: %s" % e)
    if not cfg.get("api_key"):
        cfg["api_key"] = gen_key()
        if create:
            save_config(cfg, path)
    return cfg


def save_config(cfg, path=CONFIG_PATH):
    """先写 .tmp 再原子替换，避免进程被杀时留下半截 config.json。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def mask(secret):
    secret = (secret or "").strip()
    if not secret:
        return "(空)"
    if len(secret) <= 14:
        return secret
    return secret[:10] + "…" + secret[-4:] + "(%d位)" % len(secret)


def clean_bearer(v):
    v = (v or "").strip()
    if v.lower().startswith("bearer "):
        v = v[7:].strip()
    return v


def dump_request(dump_dir, body, model_id, anthropic, tag="req"):
    """把一次请求体落盘，返回文件路径；dump_dir 为空或写失败都返回 None。

    落盘内容不含凭据（body 里本来就没有），所以可以放心直接发出来排查。
    """
    d = (dump_dir or "").strip()
    if not d:
        return None
    try:
        os.makedirs(d, exist_ok=True)
        name = "%s-%d-%s.json" % (tag, int(time.time() * 1000),
                                  re.sub(r"[^A-Za-z0-9_.-]", "_", str(model_id or "model")))
        fp = os.path.join(d, name)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump({"path": "anthropic" if anthropic else "openai",
                       "stream": bool(body.get("stream")),
                       "model": model_id, "body": body},
                      f, ensure_ascii=False, indent=2)
        return fp
    except Exception as e:
        log("  [抓包] 写文件失败: %s" % e)
        return None


def apikey_page():
    """按当前环境给出官方 API Key 的获取地址。"""
    env = (os.environ.get("CODEBUDDY_INTERNET_ENVIRONMENT") or "internal").strip().lower()
    return APIKEY_PAGES.get(env, APIKEY_PAGES["internal"])


def est_tokens(text):
    """粗略估算：中日韩按 1 字/token，其余按 4 字符/token。"""
    if not text:
        return 0
    cjk = len(re.findall(r"[\u3000-\u9fff\uff00-\uffef]", text))
    return cjk + max(0, (len(text) - cjk) + 3) // 4


def normalize_api_path(raw):
    """归一化客户端请求路径。

    常见误配：把 ANTHROPIC_BASE_URL 写成 http://127.0.0.1:8788/v1 ——
    客户端会自己再拼一层 /v1/messages，变成 /v1/v1/messages。
    这里把多出来的那层剥掉，两种写法都能用。
    """
    path = urllib.parse.urlparse(raw).path
    while path.startswith("/v1/v1/"):
        path = path[3:]
    return path or "/"


def is_official_entry(raw):
    """判断产品配置里的一条模型记录是不是"官方提供"的。

    官方模型的标志：**没有自带 url**（url 是自定义/外部模型才有的），
    模型 id 不带 custom-local:/custom: 前缀，vendor 不是 Custom/Enterprise。
    """
    if not isinstance(raw, dict) or not raw.get("id"):
        return False
    if str(raw.get("url") or "").strip():
        return False
    low = str(raw["id"]).lower()
    if any(low.startswith(p) for p in CUSTOM_PREFIXES):
        return False
    if str(raw.get("vendor") or "").strip().lower() in ("custom", "enterprise"):
        return False
    return True


# --------------------------------------------------------------------------------------
# 官方模型发现
# --------------------------------------------------------------------------------------

class ModelEntry(object):
    """一个官方模型。"""

    def __init__(self, mid, name="", vendor="", source="", supports_tool_call=True,
                 supports_images=False, supports_reasoning=False):
        self.id = mid
        self.name = name or mid
        self.vendor = vendor
        self.source = source
        self.supports_tool_call = supports_tool_call
        self.supports_images = supports_images
        self.supports_reasoning = supports_reasoning

    def capabilities(self):
        return [c for c, ok in (("tools", self.supports_tool_call),
                                ("vision", self.supports_images),
                                ("reasoning", self.supports_reasoning)) if ok]

    def to_openai(self):
        return {
            "id": self.id,
            "object": "model",
            "created": 0,
            "owned_by": self.vendor or "workbuddy",
            "x_name": self.name,
            "x_official": True,
            "x_capabilities": self.capabilities(),
            "x_source": self.source,
        }


class ModelRegistry(object):
    """只收官方模型的注册表。

    模型清单来自 WorkBuddy 本地产品配置，按 TTL 缓存；refresh() 在锁内合并
    多个来源，避免应用正在重写缓存时读到残缺清单。
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._models = []
        self._index = {}
        self._ts = 0.0
        self._sources = []
        self._skipped = 0

    def refresh(self, force=False):
        ttl = float(self.cfg.get("models_cache_ttl") or 60)
        with self._lock:
            if not force and self._models and (time.time() - self._ts) < ttl:
                return

            # **合并所有来源取并集**，不要"读到第一个就停"。
            # 原因：App 会定期重写缓存/外溢文件，某一瞬间某个文件可能只有 3 条残缺记录
            # （实测发生过：官方模型从 51 个掉到 3 个），只认单一来源会跟着残缺。
            models, sources = [], []
            skipped_ids = set()
            paths = []
            for p in PRODUCT_CONFIG_CANDIDATES:
                if p and p not in paths:
                    paths.append(p)
            for p in PRODUCT_CONFIG_FILES:
                if p not in paths:
                    paths.append(p)

            for path in paths:
                if not path or not os.path.exists(path):
                    continue
                got, sk = self._read(path)
                if not got and not sk:
                    continue
                models.extend(got)
                skipped_ids.update(sk)
                sources.append("%s（官方 %d 个 / 跳过自定义 %d 个）" % (path, len(got), len(sk)))

            merged, seen = [], set()
            for e in models:
                k = e.id.lower()
                if k in seen:
                    continue
                seen.add(k)
                merged.append(e)

            self._models = merged
            self._index = {e.id.lower(): e for e in merged}
            self._sources = sources
            self._skipped = len(skipped_ids)
            self._ts = time.time()

    def _read(self, path):
        """返回 (官方模型列表, 被跳过的自定义模型 id 集合)。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return [], set()
        if not isinstance(data, dict):
            return [], set()
        out, skipped = [], set()
        for raw in (data.get("models") or []):
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            if not is_official_entry(raw):
                skipped.add(str(raw["id"]))
                continue
            out.append(ModelEntry(
                str(raw["id"]), str(raw.get("name") or ""), str(raw.get("vendor") or ""),
                source=os.path.basename(path),
                supports_tool_call=bool(raw.get("supportsToolCall", True)),
                supports_images=bool(raw.get("supportsImages", False)),
                supports_reasoning=bool(raw.get("supportsReasoning", False))))
        return out, skipped

    @property
    def models(self):
        self.refresh()
        return list(self._models)

    @property
    def sources(self):
        self.refresh()
        return list(self._sources)

    @property
    def skipped(self):
        self.refresh()
        return self._skipped

    def get(self, mid):
        self.refresh()
        if not mid:
            return None
        key = str(mid).strip().lower()
        # 三级匹配：先要求客户端给的 id 精确命中；失败后允许分隔符差异
        # （例如 deepseek-v4-pro / deepseek_v4_pro）；最后才做包含匹配，兼容 SDK
        # 只记住了模型别名一部分的情况。
        m = self._index.get(key)
        if m:
            return m
        norm = re.sub(r"[-_.]", "", key)
        for e in self._models:
            if re.sub(r"[-_.]", "", e.id.lower()) == norm:
                return e
        for e in self._models:
            n = re.sub(r"[-_.]", "", e.id.lower())
            if norm and (norm in n or n in norm):
                return e
        return None

    def default(self):
        self.refresh()
        want = (self.cfg.get("default_model") or "").strip()
        if want:
            m = self.get(want)
            if m:
                return m
        return self._models[0] if self._models else None


# --------------------------------------------------------------------------------------
# 凭据解析（六层，谁先有就用谁）
# --------------------------------------------------------------------------------------

class Credentials(object):
    """按固定优先级解析上游凭据，并支持配置热加载和 refresh_token 续期。

    resolve() 的顺序是请求路径上最关键的行为：磁盘配置优先于环境变量，
    环境变量优先于 WorkBuddy settings 文件，helper 脚本最后兜底。
    """
    def __init__(self, cfg, config_path=None):
        self.cfg = cfg
        self.config_path = config_path
        self._mtime = 0.0
        self._helper_cache = None
        self._helper_ts = 0.0

    # ---------- 热加载 ----------

    def _reload(self):
        """config.json 被改动后立刻生效。

        写 token 的场景很常见（token 会过期，用户拿新的覆盖），如果非要重启代理才能读到，
        体验很差。这里按 mtime 热加载，--set-token 写完当前进程马上就用新的。
        """
        if not self.config_path or not os.path.exists(self.config_path):
            return
        try:
            mt = os.path.getmtime(self.config_path)
            if mt == self._mtime:
                return
            self._mtime = mt
            with open(self.config_path, "r", encoding="utf-8") as f:
                fresh = json.load(f)
        except Exception:
            return
        up = fresh.get("upstream") or {}
        cur = self.cfg.setdefault("upstream", {})
        for k in ("auth_token", "refresh_token", "api_key", "api_key_helper", "user_id",
                  "session_id", "endpoint", "path", "login_page", "extra_headers"):
            if k in up:
                cur[k] = up[k]
        # 顶层的请求侧配置也跟着热加载，否则用户改完 UA 得重启才生效（和 extra_headers 不一致）
        for k in ("user_agent",):
            if k in fresh:
                self.cfg[k] = fresh[k]
        self._helper_cache = None      # helper 可能换了，重新求值

    # ---------- 续期 ----------

    def refresh_token(self):
        """从当前内存配置读取 refresh_token；Credentials 本身会先热加载磁盘配置。"""
        return (self.cfg.get("upstream") or {}).get("refresh_token") or ""

    def try_refresh(self):
        """用 refresh_token 换新的 access_token，写回 config.json 并更新内存。

        token 过期是常态（accessToken 有效期不长），有了这个就不用手动重登 ——
        请求遇到 401/403 会自动续一次再重试。
        """
        rt = self.refresh_token()
        if not rt:
            return False
        try:
            access, new_rt = LoginClient(self.cfg).refresh(rt)
        except Exception as e:
            log("token 续期失败：%s" % e)
            return False
        up = self.cfg.setdefault("upstream", {})
        up["auth_token"] = access
        if new_rt:
            up["refresh_token"] = new_rt
        self._persist(up)
        log("token 已自动续期（%s）" % mask(access))
        return True

    def _persist(self, up):
        """把最新凭据写回 config.json，免得下次启动又用回旧的。"""
        # 不整份覆盖配置：重新读盘后只替换两个 token 字段，保留用户同时改的其他配置。
        if not self.config_path:
            return
        try:
            fresh = {}
            if os.path.exists(self.config_path):
                with open(self.config_path, "r", encoding="utf-8") as f:
                    fresh = json.load(f)
            fresh.setdefault("upstream", {})
            fresh["upstream"]["auth_token"] = up.get("auth_token", "")
            fresh["upstream"]["refresh_token"] = up.get("refresh_token", "")
            save_config(fresh, self.config_path)
            self._mtime = os.path.getmtime(self.config_path)
        except Exception as e:
            log("凭据写回 config.json 失败：%s" % e)

    # ---------- 各层 ----------

    def _from_settings_file(self):
        """读 ~/.workbuddy/settings.json 的 env / apiKeyHelper（只读，不动人家的文件）。"""
        try:
            with open(WB_SETTINGS_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
        except Exception:
            return None, None, None
        if not isinstance(s, dict):
            return None, None, None
        env = s.get("env") or {}
        token = clean_bearer(env.get("CODEBUDDY_AUTH_TOKEN")) or None
        key = clean_bearer(env.get("CODEBUDDY_API_KEY")) or None
        helper = s.get("apiKeyHelper")
        return token, key, (helper if isinstance(helper, str) and helper.strip() else None)

    def _run_helper(self, script):
        """执行 helper 脚本，stdout 第一行即凭据（与 WorkBuddy CLI 的 apiKeyHelper 语义一致）。"""
        script = (script or "").strip()
        if not script:
            return None
        if self._helper_cache and (time.time() - self._helper_ts) < 300:
            return self._helper_cache
        try:
            proc = subprocess.run(script, shell=True, capture_output=True, timeout=30)
            out = (proc.stdout or b"").decode("utf-8", "replace").strip()
            val = clean_bearer(out.splitlines()[0] if out else "")
            if val:
                self._helper_cache = val
                self._helper_ts = time.time()
                return val
            log("apiKeyHelper 没有输出内容：%s（stderr: %s）"
                % (script, (proc.stderr or b"").decode("utf-8", "replace")[:200]))
        except Exception as e:
            log("apiKeyHelper 执行失败：%s" % e)
        return None

    # ---------- 汇总 ----------

    def resolve(self):
        """返回 dict：auth_token / api_key / source / helper（按优先级取第一个命中的）。"""
        self._reload()
        up = self.cfg.get("upstream") or {}

        token = clean_bearer(up.get("auth_token"))
        if token:
            return {"auth_token": token, "api_key": clean_bearer(up.get("api_key")),
                    "source": "config.json → upstream.auth_token", "helper": None}

        api_key = clean_bearer(up.get("api_key"))
        if api_key:
            return {"auth_token": "", "api_key": api_key,
                    "source": "config.json → upstream.api_key", "helper": None}

        env_t = clean_bearer(os.environ.get("CODEBUDDY_AUTH_TOKEN"))
        if env_t:
            return {"auth_token": env_t, "api_key": "",
                    "source": "环境变量 CODEBUDDY_AUTH_TOKEN", "helper": None}
        env_k = clean_bearer(os.environ.get("CODEBUDDY_API_KEY"))
        if env_k:
            return {"auth_token": "", "api_key": env_k,
                    "source": "环境变量 CODEBUDDY_API_KEY", "helper": None}

        s_t, s_k, s_h = self._from_settings_file()
        if s_t:
            return {"auth_token": s_t, "api_key": s_k or "",
                    "source": "settings.json → env.CODEBUDDY_AUTH_TOKEN", "helper": None}
        if s_k:
            return {"auth_token": "", "api_key": s_k,
                    "source": "settings.json → env.CODEBUDDY_API_KEY", "helper": None}

        own_helper = (up.get("api_key_helper") or "").strip()
        if own_helper:
            v = self._run_helper(own_helper)
            if v:
                return {"auth_token": v, "api_key": "",
                        "source": "config.json → upstream.api_key_helper", "helper": own_helper}
        if s_h:
            v = self._run_helper(s_h)
            if v:
                return {"auth_token": v, "api_key": "",
                        "source": "settings.json → apiKeyHelper", "helper": s_h}

        return {"auth_token": "", "api_key": "", "source": "", "helper": None}

    def ready(self):
        r = self.resolve()
        return bool(r["auth_token"] or r["api_key"]), r


# --------------------------------------------------------------------------------------
# 官方登录流程（不需要会员：走产品自己的 SSO 登录，拿 accessToken）
# --------------------------------------------------------------------------------------
#
# 逆向出来的流程（与 WorkBuddy CLI 完全一致，纯轮询，不需要回调端口）：
#
#   1) POST {endpoint}/v2/plugin/auth/state?platform=workbuddy      （不带鉴权）
#        -> 返回 { state, authUrl }
#   2) 用浏览器打开 authUrl，用你自己的账号登录
#   3) GET  {endpoint}/v2/plugin/auth/token?state=<state>           （不带鉴权，轮询）
#        -> 登录完成后返回 accessToken / refreshToken
#   4) POST {endpoint}/v2/plugin/auth/token/refresh                 （用 refreshToken 续期）
#        请求头 X-Refresh-Token: <refreshToken>，X-Auth-Refresh-Source: plugin
#
# 所以本代理可以自己把 token 拿回来，并且能自动续期 —— 不需要 API Key、不需要会员、
# 也不用从 App 里抠凭据。
# --------------------------------------------------------------------------------------

NO_AUTH_HEADERS = {
    "X-No-Authorization": "true",
    "X-No-User-Id": "true",
    "X-No-Enterprise-Id": "true",
    "X-No-Department-Info": "true",
}

TOKEN_KEYS = ("accesstoken", "access_token", "token")
REFRESH_KEYS = ("refreshtoken", "refresh_token")


def _raw_http(base, method, path, body=None, headers=None, timeout=30):
    """极简 HTTP，直接连（不走本机代理），返回 (status, parsed_or_text)。"""
    u = urllib.parse.urlparse(base)
    cls = HTTPSConnection if u.scheme == "https" else HTTPConnection
    conn = cls(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=timeout)
    try:
        conn.connect()
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        h.update(headers or {})
        data = json.dumps(body).encode("utf-8") if body is not None else None
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw.decode("utf-8"))
        except Exception:
            return r.status, raw.decode("utf-8", "replace")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def find_tokens(obj):
    """从任意嵌套结构里把 accessToken / refreshToken 抠出来（字段名大小写、下划线都认）。"""
    # 上游返回结构在登录/续期接口间不完全一致，这里只靠字段名做宽松提取，
    # 找不到 refresh_token 时调用方会保留旧值。
    found = {}
    keys_seen = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                lk = str(k).lower().replace("-", "").replace("_", "")
                if isinstance(v, str) and v:
                    if lk in ("accesstoken",) and "access_token" not in found:
                        found["access_token"] = v
                    elif lk in ("refreshtoken",) and "refresh_token" not in found:
                        found["refresh_token"] = v
                    elif lk == "token" and "token" not in found:
                        found["token"] = v
                    keys_seen.append(lk)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return found


# 请求上游时遇到这些错误，视为"连接被重置/网络抖动"，自动重试
_RETRYABLE_CONN_ERRORS = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                          TimeoutError)


class _UpstreamConn(object):
    """包住上游连接：close() 时归还并发名额（配合信号量实现并发上限）。

    为什么不用 with / try-finally 在 open_stream 里释放：响应体是**流式**的，
    真正读完是在调用方（_handle_chat / complete）里，所以名额必须等到连接被
    close 时才能还 —— 这里统一挂在 close() 上，调用方已有的 finally 都不用改。
    """

    def __init__(self, conn, sem):
        self.conn = conn
        self.sem = sem

    def close(self):
        try:
            self.conn.close()
        finally:
            try:
                self.sem.release()
            except ValueError:
                pass

    def __getattr__(self, name):
        return getattr(self.conn, name)


class LoginClient(object):
    """驱动 WorkBuddy 自己的 SSO 登录流程，把 token 拿回来。

    登录流程不占用 HTTP 回调端口：先申请 state，浏览器完成登录后，
    直接用 state 轮询 token；后续续期也只依赖 refresh_token。
    """

    def __init__(self, cfg, timeout=30):
        up = cfg.get("upstream") or {}
        self.cfg = cfg
        self.endpoint = (up.get("endpoint") or DEFAULT_WB_ENDPOINT).rstrip("/")
        self.prefix = "/plugin"
        self.platform = (up.get("platform") or "workbuddy").strip()
        self.auth_path = up.get("auth_path") or "/v2/plugin/auth"
        self.login_page = (up.get("login_page") or "").strip()
        self.timeout = timeout

    # ---------- 拼登录页地址（对齐桌面端 App 实际用的那个） ----------

    def auth_url(self, state, api_auth_url=""):
        """优先用 WorkBuddy 自己的登录页模板；模板异常时退回上游返回的 authUrl。"""
        tpl = self.login_page or DEFAULT_LOGIN_PAGE
        try:
            return tpl.format(
                state=urllib.parse.quote(str(state)),
                version=(os.environ.get("WORKBUDDY_APP_VERSION") or "5.5.6").strip(),
                loginSessionId=str(uuid.uuid4()))
        except Exception as e:
            log("拼登录页地址失败（退回 API 给的地址）：%s" % e)
            return api_auth_url or ""

    # ---------- 步骤 1 ----------

    def auth_state(self):
        path = "%s/state?platform=%s" % (self.auth_path, urllib.parse.quote(self.platform))
        st, body = _raw_http(self.endpoint, "POST", path, {}, NO_AUTH_HEADERS,
                             timeout=self.timeout)
        if st != 200:
            raise UpstreamError("申请登录态失败：HTTP %s %s" % (st, str(body)[:200]), status=st)
        payload = body.get("data") if isinstance(body, dict) else None
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        if not isinstance(payload, dict):
            raise UpstreamError("登录态返回结构不认识：%s" % str(body)[:300])
        return payload

    # ---------- 步骤 3 ----------

    def poll_token(self, state, timeout=300, interval=3.0, on_tick=None):
        """轮询登录结果；用户没完成浏览器登录前，上游只会返回空数据。"""
        t0 = time.time()
        path = "%s/token?state=%s" % (self.auth_path, urllib.parse.quote(str(state)))
        while time.time() - t0 < timeout:
            time.sleep(interval)
            if on_tick:
                on_tick(time.time() - t0)
            try:
                st, body = _raw_http(self.endpoint, "GET", path, None, NO_AUTH_HEADERS,
                                     timeout=self.timeout)
            except Exception:
                continue                     # 网络抖动就继续等
            if st != 200:
                continue
            payload = body.get("data") if isinstance(body, dict) else None
            if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                payload = payload["data"]
            if not payload:
                continue                     # 还没登录完
            toks = find_tokens(payload)
            access = toks.get("access_token") or toks.get("token")
            if access:
                return access, toks.get("refresh_token") or "", payload
        raise UpstreamError("等待登录超时（%d 秒）" % timeout)

    # ---------- 步骤 4 ----------

    def refresh(self, refresh_token):
        """用 refresh_token 换新 access_token；返回 (access_token, refresh_token)。"""
        if not refresh_token:
            raise UpstreamError("没有 refresh_token，无法续期")
        path = "%s/token/refresh" % self.auth_path
        st, body = _raw_http(self.endpoint, "POST", path, {},
                             dict(NO_AUTH_HEADERS,
                                  **{"X-Refresh-Token": refresh_token,
                                     "X-Auth-Refresh-Source": "plugin"}),
                             timeout=self.timeout)
        if st != 200:
            raise UpstreamError("续期失败：HTTP %s %s" % (st, str(body)[:200]), status=st)
        payload = body.get("data") if isinstance(body, dict) else None
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        toks = find_tokens(payload or {})
        access = toks.get("access_token") or toks.get("token")
        if not access:
            raise UpstreamError("续期返回里没有 accessToken：%s" % str(payload)[:200])
        return access, (toks.get("refresh_token") or refresh_token)


# --------------------------------------------------------------------------------------
# 上游调用
# --------------------------------------------------------------------------------------

class UpstreamError(Exception):
    def __init__(self, message, status=502, body=None):
        super(UpstreamError, self).__init__(message)
        self.message = message
        self.status = status
        self.body = body


class Upstream(object):
    """负责构造上游请求头、限流连接、处理续期，并返回可读的 SSE 响应。"""

    def __init__(self, cfg, registry, creds):
        self.cfg = cfg
        self.registry = registry
        self.creds = creds
        # 限制同时打到上游的请求数：Claude Code 一轮会并行发好几个请求，
        # 并发太高会被上游直接重置连接（WinError 10054）或触发风控（code=11128）。
        self._sem = threading.BoundedSemaphore(max(1, int(cfg.get("max_upstream_concurrency") or 2)))

    def endpoint(self):
        up = self.cfg.get("upstream") or {}
        base = (up.get("endpoint") or DEFAULT_WB_ENDPOINT).rstrip("/")
        path = up.get("path") or "/v2/chat/completions"
        if not path.startswith("/"):
            path = "/" + path
        return base + path

    def headers(self):
        """每次调用都重新解析凭据，让热加载的新 token / extra_headers 立即生效。"""
        up = self.cfg.get("upstream") or {}
        cred = self.creds.resolve()
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            # 如实标识自己。别换成官方 CLI 的 UA 去"伪装渠道"—— 用一个假的官方标识
            # 既不诚实，也更容易被风控挑出来（见 README「关于请求指纹」）。
            # 想自定义就设 config.user_agent 或 upstream.extra_headers（后者优先级更高）。
            "User-Agent": (self.cfg.get("user_agent") or "").strip() or DEFAULT_USER_AGENT,
            "x-cli": "1",
            # 官方要求按版本设 CODEBUDDY_INTERNET_ENVIRONMENT（中国版=internal），
            # 这里跟着它走，默认 internal。
            "x-default-env": (os.environ.get("CODEBUDDY_INTERNET_ENVIRONMENT") or "internal").strip(),
        }
        uid = (up.get("user_id") or "").strip() or os.environ.get("CODEBUDDY_USER_ID", "")
        if uid:
            h["X-User-Id"] = uid
        sid = (up.get("session_id") or "").strip() or os.environ.get("CODEBUDDY_SESSION_ID", "")
        if sid:
            h["X-WorkBuddy-Session-Id"] = sid
        for k, v in (up.get("extra_headers") or {}).items():
            h[str(k)] = str(v)
        if cred["api_key"]:
            h["X-API-Key"] = cred["api_key"]
        if cred["auth_token"]:
            h["Authorization"] = "Bearer " + cred["auth_token"]
        elif cred["api_key"]:
            h["Authorization"] = "Bearer " + cred["api_key"]
        return h

    def open_stream(self, body, allow_refresh=True):
        """发一次请求；串行化并发 + 自动续期 + 连接被重置时重试。

        注意：
        1. WorkBuddy 后端**只支持流式**（非流式返回 400 code=11101
           "Non-stream chat request is currently not supported"），所以这里一律按流式发，
           客户端要非流式的话由调用方用 aggregate_openai_stream() 拼回去。
        2. **并发必须限制**。Claude Code 一轮会并行发好几个请求，实测 6 个并发时上游会把
           连接直接重置（WinError 10054），还会偶发风控（code=11128）。所以这里用信号量
           把同时打到上游的请求数压到 max_upstream_concurrency，超出的排队等。
        """
        body = dict(body)
        body["stream"] = True

        attempt = 0
        while True:
            attempt += 1
            if not self._sem.acquire(timeout=120):
                raise UpstreamError("上游并发已满（max_upstream_concurrency），"
                                    "请降低请求频率或调大该配置")
            try:
                conn, resp = self._open_once(body)
            except _RETRYABLE_CONN_ERRORS as e:
                self._sem.release()
                if attempt < 3:
                    log("  [上游连接被重置] 1 秒后重试（第 %d/2 次）：%s" % (attempt, str(e)[:90]))
                    time.sleep(1.0)
                    continue
                raise UpstreamError("上游连接被重置（并发太高或网络抖动），已重试 2 次：%s"
                                    % str(e)[:120])
            except Exception:
                self._sem.release()
                raise
            break

        if resp.status in (401, 403) and allow_refresh and self.creds.refresh_token():
            try:
                resp.read()
            except Exception:
                pass
            try:
                conn.close()          # close() 会归还并发名额
            except Exception:
                pass
            if self.creds.try_refresh():
                log("上游返回 %s，已自动续期，重试一次" % resp.status)
                return self.open_stream(body, allow_refresh=False)

        return _UpstreamConn(conn, self._sem), resp

    def _open_once(self, body):
        url = self.endpoint()
        u = urllib.parse.urlparse(url)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        headers = self.headers()
        if body.get("stream"):
            headers["Accept"] = "text/event-stream"
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        cls = HTTPSConnection if u.scheme == "https" else HTTPConnection
        # 连接建立和读响应使用两个超时：连接失败要尽快暴露；长回复可能持续几分钟，
        # read_timeout 必须足够大，避免流还没结束就被本地 socket 掐断。
        conn = cls(u.hostname, u.port or (443 if u.scheme == "https" else 80),
                   timeout=float(self.cfg.get("connect_timeout") or 20))
        try:
            conn.connect()
            conn.sock.settimeout(float(self.cfg.get("read_timeout") or 600))
            conn.request("POST", path, body=payload, headers=headers)
            return conn, conn.getresponse()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise

    def complete(self, model_id, body):
        """非流式取完整结果：内部仍按流式请求上游，再自己拼成完整响应。"""
        conn, resp = self.open_stream(dict(body, stream=True))
        try:
            if resp.status != 200:
                raw = resp.read()
                raise UpstreamError("上游返回 HTTP %s" % resp.status, status=resp.status,
                                    body=raw[:800].decode("utf-8", "replace"))
            return aggregate_openai_stream(resp, model_id)
        finally:
            try:
                conn.close()
            except Exception:
                pass


def iter_sse_json(resp):
    """从上游 SSE 流里逐个吐出解析好的 JSON 块。"""
    # 按行缓冲：read 返回的字节块不一定正好切在一条 data: 上，必须先攒到 \n。
    # 忽略空行/注释和非 data 帧；单个 JSON 解析失败丢弃该帧，不让整条流报废。
    buf = b""
    while True:
        try:
            chunk = resp.read(256)
        except Exception:
            break
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line or line.startswith(b":") or not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload in (b"[DONE]", b""):
                continue
            try:
                yield json.loads(payload.decode("utf-8"))
            except Exception:
                continue


def aggregate_openai_stream(resp, model):
    """把 OpenAI 风格的 SSE 增量流拼回一个完整的 chat.completion 对象。

    上游只给流式，所以客户端要非流式时必须在这里拼：正文、思考过程、工具调用的
    arguments 都是分片来的，要按 index 归并、把字符串接起来。
    """
    text_parts, think_parts = [], []
    tool_calls = {}
    finish_reason, usage, cid, cmodel = None, {}, None, model

    for obj in iter_sse_json(resp):
        if obj.get("id"):
            cid = obj["id"]
        if obj.get("model"):
            cmodel = obj["model"]
        if obj.get("usage"):
            usage = obj["usage"]
        for ch in (obj.get("choices") or []):
            d = ch.get("delta") or {}
            if d.get("content"):
                text_parts.append(d["content"])
            r = d.get("reasoning_content") or d.get("reasoning")
            if r:
                think_parts.append(r)
            for tc in (d.get("tool_calls") or []):
                i = tc.get("index", 0)
                slot = tool_calls.setdefault(i, {"id": "", "type": "function",
                                                 "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if ch.get("finish_reason"):
                finish_reason = ch["finish_reason"]

    msg = {"role": "assistant", "content": "".join(text_parts) or None}
    if think_parts:
        msg["reasoning_content"] = "".join(think_parts)
    if tool_calls:
        msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]

    out = {
        "id": cid or ("chatcmpl-%s" % secrets.token_hex(8)),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": cmodel,
        "choices": [{
            "index": 0,
            "message": msg,
            "finish_reason": finish_reason or ("tool_calls" if tool_calls else "stop"),
        }],
    }
    if usage:
        out["usage"] = usage
    return out


# --------------------------------------------------------------------------------------
# Anthropic <-> OpenAI 协议转换
# --------------------------------------------------------------------------------------

def _flatten_text(content):
    """把 Anthropic 的 string 或 content-block 列表压成一个纯文本串。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text") or "")
        elif t == "thinking":
            parts.append(b.get("thinking") or "")
        elif t == "tool_result":
            inner = b.get("content")
            parts.append(_flatten_text(inner) if isinstance(inner, list)
                         else ("" if inner is None else str(inner)))
        elif t == "image":
            parts.append("[图片]")
    return "".join(parts)


def anthropic_to_openai(body, default_model):
    """Anthropic /v1/messages 请求体 -> OpenAI /v1/chat/completions 请求体。"""
    messages = []

    system = body.get("system")
    if system:
        messages.append({"role": "system", "content": _flatten_text(system)})

    for m in (body.get("messages") or []):
        role = m.get("role")
        content = m.get("content")

        if role == "assistant":
            # Anthropic 的 assistant 历史里，文本和 tool_use 是同级 content block；
            # OpenAI 则把工具调用挂在 message.tool_calls 上，文本仍留在 content。
            text_parts, tool_calls = [], []
            if isinstance(content, str):
                text_parts.append(content)
            else:
                for b in (content or []):
                    if not isinstance(b, dict):
                        continue
                    t = b.get("type")
                    if t == "text":
                        text_parts.append(b.get("text") or "")
                    elif t == "tool_use":
                        tool_calls.append({
                            "id": b.get("id") or ("call_%s" % len(tool_calls)),
                            "type": "function",
                            "function": {"name": b.get("name") or "",
                                         "arguments": json.dumps(b.get("input") or {},
                                                                 ensure_ascii=False)},
                        })
            msg = {"role": "assistant", "content": "".join(text_parts) or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            messages.append(msg)
            continue

        if isinstance(content, str):
            messages.append({"role": "user", "content": content})
            continue

        # 用户轮里可能同时出现普通输入和 tool_result。普通输入映射成 user 消息，
        # tool_result 映射成 OpenAI 的 tool 消息，且必须带回原 tool_use_id。
        pending, results = [], []
        for b in (content or []):
            if not isinstance(b, dict):
                continue
            (results if b.get("type") == "tool_result" else pending).append(b)
        if pending:
            text = _flatten_text(pending)
            if text:
                messages.append({"role": "user", "content": text})
        for b in results:
            inner = b.get("content")
            messages.append({
                "role": "tool",
                "tool_call_id": b.get("tool_use_id") or "",
                "content": _flatten_text(inner) if isinstance(inner, list)
                else ("" if inner is None else str(inner)),
            })

    out = {
        "model": body.get("model") or default_model,
        "messages": messages,
        "stream": bool(body.get("stream")),
    }
    for k in ("max_tokens", "temperature", "top_p"):
        if body.get(k) is not None:
            out[k] = body[k]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]

    tools = body.get("tools")
    if tools:
        out["tools"] = [{
            "type": "function",
            "function": {"name": t.get("name"),
                         "description": t.get("description") or "",
                         "parameters": t.get("input_schema") or {"type": "object",
                                                                  "properties": {}}},
        } for t in tools if isinstance(t, dict) and t.get("name")]

    tc = body.get("tool_choice")
    if tc and out.get("tools"):
        t = tc.get("type")
        if t == "auto":
            out["tool_choice"] = "auto"
        elif t == "any":
            out["tool_choice"] = "required"
        elif t == "none":
            out["tool_choice"] = "none"
        elif t == "tool" and tc.get("name"):
            out["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
    return out


STOP_MAP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
            "function_call": "tool_use", "content_filter": "end_turn"}


def openai_to_anthropic(data, model):
    """OpenAI 非流式响应 -> Anthropic /v1/messages 响应。"""
    # Anthropic 的 content 是有顺序的 block 列表；这里保持 thinking → text →
    # tool_use 的出现顺序，并把 OpenAI 的 JSON arguments 字符串还原成对象。
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    blocks = []

    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if reasoning:
        blocks.append({"type": "thinking", "thinking": reasoning, "signature": ""})
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
            except Exception:
                args = {"_raw": raw_args}
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            args = {}
        blocks.append({"type": "tool_use",
                       "id": tc.get("id") or ("toolu_%s" % secrets.token_hex(8)),
                       "name": fn.get("name") or "", "input": args})
    if not blocks:
        blocks.append({"type": "text", "text": ""})

    usage = data.get("usage") or {}
    return {
        "id": data.get("id") or ("msg_%s" % secrets.token_hex(10)),
        "type": "message", "role": "assistant", "model": model,
        "content": blocks,
        "stop_reason": STOP_MAP.get(choice.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0)},
    }


class AnthropicStreamAdapter(object):
    """把 OpenAI 的 SSE 增量流翻译成 Anthropic 的 SSE 事件序列。

    OpenAI:    data: {"choices":[{"delta":{"content":...},"finish_reason":...}]}
    Anthropic: message_start → ping →
               (content_block_start → *_delta → content_block_stop)* →
               message_delta → message_stop
    """

    def __init__(self, model, input_tokens=0):
        self.model = model
        self.msg_id = "msg_%s" % secrets.token_hex(10)
        self.input_tokens = input_tokens
        self.next_index = 0
        self.open_block = None
        self.text_index = None
        self.think_index = None
        self.tool_blocks = {}
        self.output_tokens = 0
        self.stop_reason = "end_turn"
        self._buf = b""

    @staticmethod
    def _ev(event, obj):
        return ("event: %s\ndata: %s\n\n"
                % (event, json.dumps(obj, ensure_ascii=False))).encode("utf-8")

    def start(self):
        yield self._ev("message_start", {
            "type": "message_start",
            "message": {"id": self.msg_id, "type": "message", "role": "assistant",
                        "model": self.model, "content": [],
                        "stop_reason": None, "stop_sequence": None,
                        "usage": {"input_tokens": self.input_tokens, "output_tokens": 0}}})
        yield self._ev("ping", {"type": "ping"})

    def _close_open(self):
        if self.open_block is not None:
            idx, _ = self.open_block
            self.open_block = None
            return self._ev("content_block_stop", {"type": "content_block_stop", "index": idx})
        return None

    def feed(self, chunk_obj):
        out = []
        # 一个 OpenAI delta 里三种内容（reasoning / text / tool_calls）不一定互斥。
        # Anthropic 一次只能有一个打开的 content block，所以切类型前必须先 close 旧块。
        choice = (chunk_obj.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}

        # 思考过程 → thinking 块
        think = delta.get("reasoning_content") or delta.get("reasoning")
        if think:
            if self.think_index is None:
                ev = self._close_open()
                if ev:
                    out.append(ev)
                idx = self.next_index
                self.next_index += 1
                self.think_index = idx
                self.open_block = (idx, "thinking")
                out.append(self._ev("content_block_start", {
                    "type": "content_block_start", "index": idx,
                    "content_block": {"type": "thinking", "thinking": ""}}))
            out.append(self._ev("content_block_delta", {
                "type": "content_block_delta", "index": self.think_index,
                "delta": {"type": "thinking_delta", "thinking": think}}))

        # 正文 → text 块
        text = delta.get("content")
        if text:
            if self.text_index is None:
                ev = self._close_open()
                if ev:
                    out.append(ev)
                idx = self.next_index
                self.next_index += 1
                self.text_index = idx
                self.open_block = (idx, "text")
                out.append(self._ev("content_block_start", {
                    "type": "content_block_start", "index": idx,
                    "content_block": {"type": "text", "text": ""}}))
            out.append(self._ev("content_block_delta", {
                "type": "content_block_delta", "index": self.text_index,
                "delta": {"type": "text_delta", "text": text}}))

        # 工具调用 → tool_use 块
        for tc in (delta.get("tool_calls") or []):
            oi = tc.get("index", 0)
            if oi not in self.tool_blocks:
                ev = self._close_open()
                if ev:
                    out.append(ev)
                idx = self.next_index
                self.next_index += 1
                self.tool_blocks[oi] = idx
                self.open_block = (idx, "tool_use")
                fn = tc.get("function") or {}
                out.append(self._ev("content_block_start", {
                    "type": "content_block_start", "index": idx,
                    "content_block": {"type": "tool_use",
                                      "id": tc.get("id") or ("toolu_%s" % secrets.token_hex(8)),
                                      "name": fn.get("name") or "", "input": {}}}))
            args = (tc.get("function") or {}).get("arguments")
            if args:
                out.append(self._ev("content_block_delta", {
                    "type": "content_block_delta", "index": self.tool_blocks[oi],
                    "delta": {"type": "input_json_delta", "partial_json": args}}))

        fr = choice.get("finish_reason")
        if fr:
            self.stop_reason = STOP_MAP.get(fr, "end_turn")
        usage = chunk_obj.get("usage")
        if usage:
            self.output_tokens = usage.get("completion_tokens", self.output_tokens)
            if usage.get("prompt_tokens"):
                self.input_tokens = usage["prompt_tokens"]
        return out

    def finish(self):
        out = []
        # 无论上游正常结束还是断流，都要补齐 block stop 和 message 终止事件，
        # 否则 Anthropic SDK 会一直等不到完整消息。
        ev = self._close_open()
        if ev:
            out.append(ev)
        out.append(self._ev("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": self.stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": self.output_tokens}}))
        out.append(self._ev("message_stop", {"type": "message_stop"}))
        return out

    def feed_sse_bytes(self, data):
        """喂原始 SSE 字节，产出 (anthropic 字节, 是否结束)。"""
        self._buf += data
        out, done = b"", False
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if not line or line.startswith(b":") or not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload in (b"[DONE]", b""):
                done = True
                continue
            try:
                obj = json.loads(payload.decode("utf-8"))
            except Exception:
                continue
            for ev in self.feed(obj):
                out += ev
        return out, done


def openai_sse_to_anthropic_chunks(resp, model, input_tokens=0):
    """把上游的 OpenAI SSE 逐块转成 Anthropic SSE 字节流（生成器）。"""
    ad = AnthropicStreamAdapter(model, input_tokens)
    for ev in ad.start():
        yield ev
    try:
        while True:
            try:
                chunk = resp.read(256)
            except Exception:
                break
            if not chunk:
                break
            out, done = ad.feed_sse_bytes(chunk)
            if out:
                yield out
            if done:
                break
    finally:
        for ev in ad.finish():
            yield ev


# --------------------------------------------------------------------------------------
# 对外 HTTP 服务
# --------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    """对外暴露 OpenAI / Anthropic 兼容接口的 HTTP 层。

    所有共享对象挂在 Server 上；_sent 用来区分“尚未响应”和“SSE 已开始”，
    异常处理会据此决定是发 JSON 错误，还是只能补 chunked 结束符。
    """

    protocol_version = "HTTP/1.1"
    server_version = "wb-model-proxy/2.0"
    sys_version = ""

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            log("  %s - %s" % (self.address_string(), fmt % args))

    @property
    def cfg(self):
        return self.server.cfg

    @property
    def registry(self):
        return self.server.registry

    @property
    def upstream(self):
        return self.server.upstream

    # ---------- 鉴权 / 基础 ----------

    def _client_key(self):
        # OpenAI 客户端常用 Authorization: Bearer；Anthropic 客户端常用 x-api-key。
        # 两种都接受，后续用常数时间比较，避免 key 比较变成可计时侧信道。
        h = self.headers.get("Authorization") or ""
        k = h[7:].strip() if h.lower().startswith("bearer ") else ""
        return k or (self.headers.get("x-api-key") or "").strip()

    def _authed(self):
        if self.cfg.get("allow_any_key"):
            return True
        import hmac
        given = self._client_key()
        want = self.cfg.get("api_key") or ""
        if given and want and hmac.compare_digest(given, want):
            return True
        self._send_json(401, {"error": {
            "type": "authentication_error",
            "message": "无效的 API Key。本代理只认 config.json 里的 api_key：%s" % want}})
        return False

    def _send_json(self, status, obj, extra=None):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        # _sent 必须在开始写响应头前置位；后续异常不能对同一个连接再写第二份响应。
        self._sent = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise ValueError("请求体不是合法 JSON: %s" % e)

    # ---------- GET ----------

    def do_GET(self):
        self._sent = False
        path = normalize_api_path(self.path).rstrip("/") or "/"
        if path in ("/", "/health", "/status"):
            return self._health()
        if path == "/v1/models":
            if not self._authed():
                return
            self.registry.refresh()
            return self._send_json(200, {
                "object": "list",
                "data": [m.to_openai() for m in self.registry.models]})
        self._send_json(404, {"error": {"type": "invalid_request_error",
                                        "message": "未知路径: " + path}})

    def _health(self):
        self.registry.refresh()
        ready, cred = self.upstream.creds.ready()
        self._send_json(200, {
            "service": "wb-model-proxy",
            "version": "2.0",
            "scope": "WorkBuddy 官方模型",
            "uptime_s": round(time.time() - self.server.started_at, 1),
            "endpoints": ["/v1/models", "/v1/chat/completions", "/v1/messages",
                          "/v1/messages/count_tokens"],
            "official_models": len(self.registry.models),
            "skipped_custom_models": self.registry.skipped,
            "upstream": self.upstream.endpoint(),
            "credential": {
                "configured": ready,
                "source": cred["source"] or "(未配置)",
                "auth_token": mask(cred["auth_token"]),
                "api_key": mask(cred["api_key"]),
            },
            "model_sources": self.registry.sources,
        })

    # ---------- POST ----------

    def do_POST(self):
        self._sent = False
        path = normalize_api_path(self.path)
        try:
            body = self._read_body()
        except ValueError as e:
            return self._send_json(400, {"error": {"type": "invalid_request_error",
                                                   "message": str(e)}})
        if path == "/v1/messages/count_tokens":
            if not self._authed():
                return
            return self._count_tokens(body)
        if path not in ("/v1/chat/completions", "/v1/completions", "/v1/messages"):
            return self._send_json(404, {"error": {"type": "invalid_request_error",
                                                   "message": "未知路径: " + path}})
        if not self._authed():
            return
        return self._handle_chat(body, anthropic=(path == "/v1/messages"))

    def _count_tokens(self, body):
        text = _flatten_text(body.get("system"))
        for m in (body.get("messages") or []):
            text += _flatten_text(m.get("content"))
        for t in (body.get("tools") or []):
            text += json.dumps(t, ensure_ascii=False)
        return self._send_json(200, {"input_tokens": max(1, est_tokens(text))})

    # ---------- 主流程 ----------

    def _handle_chat(self, body, anthropic):
        """把一次对外协议请求转发到上游，并按原协议返回流式或完整结果。

        这里是所有 POST 聊天请求的总入口：模型解析、凭据检查、协议转换、
        上游瞬时错误重试和 SSE 输出都汇合在这一段。
        """
        want = body.get("model") or ""
        entry = self.registry.get(want) or self.registry.default()
        # 客户端传了不认识的模型时不硬拒：退回配置的默认官方模型，兼容 SDK 默认值。
        if entry is None:
            return self._send_json(503, {"error": {
                "type": "api_error", "code": "no_models",
                "message": "没读到任何 WorkBuddy 官方模型。用 --list 看模型清单是否读到。"}})

        ready, cred = self.upstream.creds.ready()
        if not ready:
            return self._send_json(503, {"error": {
                "type": "api_error", "code": "credential_missing",
                "message": ("官方模型还没配凭据。**最省事的方式（不需要会员、也不需要 API Key）**：\n"
                            "  python wb_proxy.py --login     # 用浏览器走一次官方登录，token 自动存好\n"
                            "然后 python wb_proxy.py --check 验证。\n"
                            "有会员的话也可以走官方 API Key：%s → --set-api-key；\n"
                            "或直接设环境变量 CODEBUDDY_API_KEY。\n"
                            "查当前凭据来自哪一层：python wb_proxy.py --where"
                            % apikey_page())}})

        fp = dump_request(self.cfg.get("dump_dir"), body, entry.id, anthropic, tag="req")
        if fp:
            log("  [抓包] 已落盘 %s" % fp)

        want_stream = bool(body.get("stream"))
        up_body = anthropic_to_openai(body, entry.id) if anthropic else dict(body)
        up_body["model"] = entry.id
        # 上游只支持流式，交给 open_stream() 去强制；客户端要非流式就在这里自己拼

        # 上游的安全策略（code=11128 "Illegal API invocation from an unapproved channel"）
        # 是**偶发**的，而且已经用控制变量把这几种解释全排除了：请求体体积（8B~32KB）、
        # 内容里的 Claude Code 品牌词、tools / system / max_tokens（8~4096）、
        # 串行 / 6 路并发、以及连续 60 次的高频量（60/60 全 200）。
        # 它的特征是**成串出现**：同一时刻连续 3~4 次被拦、跨度 > 23s，窗口过去立刻恢复。
        # Claude Code 对 400 不会重试，所以这里替它重试；退避间隔 = transient_retry_waits。
        TRANSIENT_BIZ_CODES = (11128,)
        RETRY_WAITS = transient_retry_waits(self.cfg)
        # 触发条件至今没法稳定复现 → 被拦时自动把现场请求体落盘，下次直接看现场
        ON_11128_DUMP_DIR = ((self.cfg.get("dump_11128_dir") or "").strip()
                             or os.path.join(SCRIPT_DIR, "run", "dumps"))

        extra = {"X-Proxy-Model": entry.id, "X-Proxy-Scope": "official",
                 "Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        try:
            attempt = 0
            while True:
                attempt += 1
                try:
                    conn, resp = self.upstream.open_stream(up_body)
                except Exception as e:
                    return self._send_json(502, {"error": {
                        "type": "api_error",
                        "message": "连不上 WorkBuddy 后端 (%s)：%s"
                                   % (self.upstream.endpoint(), e)}})

                if resp.status == 200:
                    raw_err = None
                else:
                    raw_err = resp.read()
                    biz = None
                    try:
                        j = json.loads(raw_err.decode("utf-8", "replace"))
                        if isinstance(j, dict):
                            biz = j.get("code")
                    except Exception:
                        biz = None
                    try:
                        conn.close()
                    except Exception:
                        pass
                    if biz in TRANSIENT_BIZ_CODES and attempt <= len(RETRY_WAITS):
                        # 首次被拦就把现场落盘（只落一次，避免重试把目录刷满）
                        if attempt == 1 and self.cfg.get("dump_on_11128", True):
                            dfp = dump_request(ON_11128_DUMP_DIR, body, entry.id,
                                               anthropic, tag="11128")
                            if dfp:
                                log("  [11128 现场] 请求体已落盘 %s（直接发我即可定位）" % dfp)
                        wait = RETRY_WAITS[attempt - 1]
                        log("  [安全策略拦截 code=%s] %g 秒后自动重试（第 %d/%d 次）"
                            % (biz, wait, attempt, len(RETRY_WAITS)))
                        time.sleep(wait)
                        continue
                    break        # 非瞬时错误，走错误返回
                break

            # conn/resp 的生命周期由外层 finally 兜底。只有 HTTP 200 时 resp
            # 仍持有未读完的 SSE 流；非 200 已在上面读出错误体并关闭连接。
            if raw_err is not None:
                text = raw_err[:1200].decode("utf-8", "replace")
                hint = ""
                if resp.status in (401, 403):
                    hint = "（凭据无效或已过期 → 重新 --login）"
                elif resp.status == 504:
                    hint = "（后端说 API key 校验服务不可用，通常意味着这把凭据不被接受）"
                elif resp.status == 429:
                    hint = "（该模型额度用完或触发限频；换个模型往往能继续用）"
                if anthropic:
                    return self._send_json(resp.status, {"type": "error", "error": {
                        "type": "api_error",
                        "message": "上游返回 %s %s %s" % (resp.status, hint, text[:300])}})
                return self._send_json(resp.status, {"error": {
                    "type": "upstream_error", "code": resp.status,
                    "message": "上游返回 HTTP %s %s" % (resp.status, hint),
                    "upstream_body": text}})

            if anthropic:
                if want_stream:
                    return self._stream_anthropic(resp, entry, extra)
                data = aggregate_openai_stream(resp, entry.id)
                return self._send_json(200, openai_to_anthropic(data, entry.id), extra)

            if want_stream:
                return self._stream_openai(resp, extra)
            data = aggregate_openai_stream(resp, entry.id)
            return self._send_json(200, data, extra)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as e:
            log("  [转发异常] %s" % e)
            # 响应已经发出去一部分时，绝不能再往里写 JSON 错误体 —— 否则客户端会在
            # SSE 流中间读到一段 HTTP 响应头而解析崩溃。只补一个分块结束符收尾。
            if getattr(self, "_sent", False):
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except Exception:
                    pass
                self.close_connection = True
            else:
                try:
                    self._send_json(500, {"error": {"type": "api_error",
                                                    "message": "代理内部错误: %s" % e}})
                except Exception:
                    pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _chunked_start(self, content_type, extra):
        # SSE 读完上游再结束时长度未知，用 chunked 既能立即推送，也能安全收尾。
        self._sent = True
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Transfer-Encoding", "chunked")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()

    def _write_chunk(self, data):
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")

    def _stream_openai(self, resp, extra):
        """OpenAI 侧：原样透传上游 SSE。"""
        self._chunked_start(resp.getheader("Content-Type") or "text/event-stream", extra)
        while True:
            chunk = resp.read(512)
            if not chunk:
                break
            self._write_chunk(chunk)
        self.wfile.write(b"0\r\n\r\n")
        try:
            self.wfile.flush()
        except Exception:
            pass

    def _stream_anthropic(self, resp, entry, extra):
        """Anthropic 侧：把上游 OpenAI SSE 翻译成 Anthropic 事件流。"""
        self._chunked_start("text/event-stream", extra)
        for piece in openai_sse_to_anthropic_chunks(resp, entry.id):
            self._write_chunk(piece)
        self.wfile.write(b"0\r\n\r\n")
        try:
            self.wfile.flush()
        except Exception:
            pass


class Server(ThreadingHTTPServer):
    """每个请求一个线程；同时把 Handler 需要的 registry/creds/upstream 挂在自己身上。"""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """静音 keep-alive 连接被客户端断开时的噪音日志。

        Claude Code 每次请求完会直接断开连接，socketserver 会在后台线程里打一大段
        ConnectionResetError traceback —— 那是正常现象，不是错误，别让它刷屏吓人。
        """
        et = sys.exc_info()[0]
        if et in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError):
            return
        super().handle_error(request, client_address)

    def __init__(self, addr, handler, cfg, registry, upstream, verbose=False):
        self.cfg = cfg
        self.registry = registry
        self.upstream = upstream
        self.started_at = time.time()
        self.verbose = verbose
        ThreadingHTTPServer.__init__(self, addr, handler)


# --------------------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------------------

def cmd_list(registry, upstream):
    registry.refresh(force=True)
    models = registry.models
    print("\n模型来源：")
    for s in (registry.sources or ["(没读到任何产品配置)"]):
        print("   -", s)
    if registry.skipped:
        print("   已跳过 %d 个自定义/外部模型（带 url、custom- 前缀或 vendor=Custom 的）"
              % registry.skipped)

    ready, cred = upstream.creds.ready()
    print("\n凭据：%s" % (cred["source"] if ready else "(未配置)"))
    if ready:
        print("      auth_token=%s  api_key=%s"
              % (mask(cred["auth_token"]), mask(cred["api_key"])))

    print("\nWorkBuddy 官方模型共 %d 个：" % len(models))
    print("-" * 82)
    print("%-32s %-8s %-26s %s" % ("模型 id", "vendor", "名称", "能力"))
    print("-" * 82)
    for m in models:
        print("%-32s %-8s %-26s %s" % (m.id, m.vendor, m.name[:24], ",".join(m.capabilities())))
    print("-" * 82 + "\n")
    return 0


def cmd_where(upstream):
    ready, cred = upstream.creds.ready()
    print("\n=== 凭据来源 ===")
    print("  上游端点   %s" % upstream.endpoint())
    if ready:
        print("  来源       %s" % cred["source"])
        print("  auth_token %s" % mask(cred["auth_token"]))
        print("  api_key    %s" % mask(cred["api_key"]))
        if cred.get("helper"):
            print("  helper     %s" % cred["helper"])
    else:
        print("  来源       (未配置)")
    print("\n  查找顺序：")
    print("   1) config.json → upstream.auth_token        ← python wb_proxy.py --set-token")
    print("   2) config.json → upstream.api_key           ← python wb_proxy.py --set-api-key")
    print("   3) 环境变量 CODEBUDDY_AUTH_TOKEN / CODEBUDDY_API_KEY")
    print("   4) %s 的 env.CODEBUDDY_AUTH_TOKEN / env.CODEBUDDY_API_KEY" % WB_SETTINGS_FILE)
    print("   5) 上面的 apiKeyHelper，或 config.json → upstream.api_key_helper")
    print("\n  官方 API Key 获取地址：%s" % apikey_page())
    print("  （官方优先级也是 AUTH_TOKEN > apiKeyHelper > API_KEY，和这里一致）")
    print()
    return 0


def cmd_headers(cfg, upstream):
    """打印代理**实际**会发给上游的请求头（凭据脱敏）。

    想知道"我的代理到底发了什么头"就看它 —— 比猜官方客户端带什么更有用。
    """
    print("\n=== 上游请求头（%s）===" % upstream.endpoint())
    h = upstream.headers()
    for k in sorted(h, key=lambda s: s.lower()):
        v = h[k]
        if k.lower() == "authorization":
            v = "Bearer " + mask(clean_bearer(v))
        elif k.lower() == "x-api-key":
            v = mask(v)
        print("  %-22s %s" % (k, v))
    print("\n  想加/改任何头：`config.json → upstream.extra_headers`，"
          "例如 {\"User-Agent\": \"...\"}；")
    print("  也可以只改 UA：`config.json → user_agent`。改完不用重启，下一次请求就生效。")
    print("  ⚠️ 用假的官方 UA / 指纹去伪装渠道属于糊弄厂商的反滥用控制，而且**不解决问题**"
          "（实测 11128 与请求头无关）—— 详见 README「关于请求指纹」。")
    print()
    return 0


def cmd_check(cfg, registry, upstream):
    registry.refresh(force=True)
    print("\n=== 体检 ===")
    print("上游端点     %s" % upstream.endpoint())
    ready, cred = upstream.creds.ready()
    print("凭据         %s" % (cred["source"] if ready else "(未配置)"))
    if ready:
        print("             auth_token=%s  api_key=%s"
              % (mask(cred["auth_token"]), mask(cred["api_key"])))
    models = registry.models
    print("官方模型     %d 个（已跳过自定义/外部 %d 个）" % (len(models), registry.skipped))

    if not models:
        print("\n[失败] 一个官方模型都没读到 —— 用 --list 看是不是路径不对。")
        return 1
    if not ready:
        print("\n[跳过] 没配凭据。最省事的方式（不需要会员、不需要 API Key）：")
        print("       python wb_proxy.py --login     # 浏览器走一次官方登录")
        print("       然后重跑 --check")
        return 1

    model = registry.default()
    print("\n--- 用官方模型 %s 真发一次请求 ---" % model.id)
    try:
        data = upstream.complete(model.id, {
            "model": model.id, "stream": False, "max_tokens": 600,
            "messages": [{"role": "user", "content": "只回答两个字：收到"}]})
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        content = (msg.get("content") or "").strip()
        reasoning = (msg.get("reasoning_content") or "").strip()
        print("  [通过] HTTP 200")
        print("         正文     : %s" % (content[:80] or "(空)"))
        if reasoning:
            print("         思考过程 : %s" % reasoning[:60].replace("\n", " "))
        return 0
    except UpstreamError as e:
        print("  [失败] HTTP %s  %s" % (e.status, (e.body or e.message)[:400]))
        if e.status in (401, 403):
            print("         凭据无效或过期 —— 重新取一个 token 再 --set-token 覆盖。")
        elif e.status == 504:
            print("         后端回 'API key verification service unavailable'，"
                  "通常意味着这把凭据不被接受。")
        return 1
    except Exception as e:
        print("  [失败] %s: %s" % (type(e).__name__, e))
        return 1


def find_user_id(obj):
    """从返回结构里找 userId / uid（有的上游接口会要 X-User-Id）。"""
    found = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                lk = str(k).lower()
                if lk in ("uid", "userid", "user_id") and isinstance(v, str) and v:
                    found.append(v)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return found[0] if found else ""


def cmd_login(cfg, cfg_path, no_browser=False, timeout=300, interval=3.0):
    lc = LoginClient(cfg)
    print("\n=== 走 WorkBuddy 官方登录流程拿 token（不需要会员、不需要 API Key）===")
    print("上游 %s" % lc.endpoint)
    try:
        state = lc.auth_state()
    except Exception as e:
        print("[失败] 申请登录态失败：%s" % e)
        return 1

    url = state.get("authUrl") or state.get("auth_url") or state.get("url") or ""
    st = state.get("state") or state.get("authState") or state.get("sign") or ""
    if not st:
        print("[失败] 返回里没找到 state：%s"
              % json.dumps(state, ensure_ascii=False)[:400])
        return 1
    # 用桌面端 App 实际使用的 workbuddy.cn 登录页（API 给的 authUrl 会跳到 codebuddy.cn，
    # 那是另一套账号体系，登了也绑不到这个 state 上）
    url = lc.auth_url(st, api_auth_url=url)

    print("1) 已申请到登录态 state=%s" % (str(st)[:20] + ("…" if len(str(st)) > 20 else "")))
    print("\n2) 用浏览器打开下面这个地址，用你平时登录 WorkBuddy 的**同一个账号**登录：\n")
    print("   %s\n" % url)
    if not no_browser:
        try:
            import webbrowser
            webbrowser.open(url)
            print("   （已尝试自动打开浏览器；没弹出来就手动复制上面这行）")
        except Exception:
            pass
    print("\n3) 登录完成后不用做别的，本脚本会自动把 token 取回来（最多等 %d 秒）...\n"
          % timeout)

    def on_tick(elapsed):
        print("   等待中… %ds " % int(elapsed), end="\r", flush=True)

    try:
        access, refresh, raw = lc.poll_token(st, timeout=timeout, interval=interval,
                                              on_tick=on_tick)
    except Exception as e:
        print("\n[失败] %s" % e)
        return 1
    print(" " * 50, end="\r")

    fresh = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                fresh = json.load(f)
        except Exception:
            fresh = {}
    fresh.setdefault("upstream", {})
    fresh["upstream"]["auth_token"] = access
    if refresh:
        fresh["upstream"]["refresh_token"] = refresh
    uid = find_user_id(raw)
    if uid and not fresh["upstream"].get("user_id"):
        fresh["upstream"]["user_id"] = uid
    save_config(fresh, cfg_path)

    cfg.setdefault("upstream", {}).update(fresh["upstream"])
    print("4) 登录成功！")
    print("   auth_token    %s" % mask(access))
    print("   refresh_token %s" % (mask(refresh) if refresh else "(没拿到，过期后需重新 --login)"))
    if uid:
        print("   user_id       %s" % uid)
    print("   已写入 %s" % cfg_path)
    print("\n" + "!" * 62)
    print("!!  注意：--login 只是拿到了 token，**代理还没有启动**！！")
    print("!!  现在必须运行：start-bg.bat    （后台常驻，推荐）")
    print("!!  或者：        python wb_proxy.py")
    print("!!  不启动的话，Claude Code 一定报 Connection refused")
    print("!" * 62)
    print("\n验证：python wb_proxy.py --check")
    print("token 过期会自动用 refresh_token 续期，不用管。\n")
    return 0


def cmd_refresh(cfg, cfg_path):
    creds = Credentials(cfg, cfg_path)
    if not creds.refresh_token():
        print("没有 refresh_token，无法续期。先跑：python wb_proxy.py --login")
        return 1
    print("正在用 refresh_token 续期 ...")
    if creds.try_refresh():
        ready, cred = creds.ready()
        print("[通过] 新的 auth_token：%s" % mask(cred["auth_token"]))
        return 0
    print("[失败] 续期没成功，可能 refresh_token 也过期了 —— 重新跑 --login")
    return 1


CAT = r"""
  __      __ ___     __  __          _      ___  ___  ___
 / / /\ \ \| _ )   |  \/  |___  _ __| |___ | _ \/ _ \/ _ \ \/ /
 \ \/  \/ /| _ \   | |\/| / _ \| '_ \ / -_)|  _/ (_) | (_) >  <
  \__/\__/ |___/   |_|  |_\___/| .__/_\___||_|  \___/ \___/_/\_\
                              |_|
"""


def print_banner(cfg, registry, upstream):
    base = "http://%s:%d" % (cfg["host"], cfg["port"])
    ready, cred = upstream.creds.ready()
    print(CAT)
    print("  WorkBuddy 官方模型 → 本地统一接口")
    print("  " + "-" * 62)
    print("  OpenAI 兼容   %s/v1     (/chat/completions)" % base)
    print("  Anthropic     %s/v1     (/messages  ← Claude Code 用这个)" % base)
    print("  模型列表      %s/v1/models" % base)
    print("  状态          %s/health" % base)
    print("  API Key       %s" % cfg["api_key"])
    print("  " + "-" * 62)
    print("  官方模型      %d 个（跳过自定义/外部 %d 个）"
          % (len(registry.models), registry.skipped))
    print("  上游          %s" % upstream.endpoint())
    print("  凭据          %s" % (cred["source"] if ready else "未配置"))
    print("  " + "-" * 62)
    if not ready:
        print("  [!] 官方模型还没配凭据。最省事的一条命令（不需要会员、不需要 API Key）：")
        print("      python wb_proxy.py --login")
        print("      ↑ 会用浏览器走一次官方登录，token 自动存好，过期还会自动续期")
        print("      有会员的话也可以用官方 API Key：%s" % apikey_page())
        print("      → python wb_proxy.py --set-api-key \"<key>\"")
        print("      看当前凭据来自哪一层：python wb_proxy.py --where")
        print("  " + "-" * 62)
    print("  文档：README.md        退出：Ctrl+C\n")


# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------

def main():
    """启动入口和一次性命令的分发器。

    顺序很重要：先构造 registry/creds/upstream，再执行登录、刷新、检查类命令；
    没有子命令时才进入本地 HTTP 服务，缺凭据时会自动引导一次登录。
    """
    ap = argparse.ArgumentParser(
        description="把 WorkBuddy 官方模型暴露成本地 OpenAI / Anthropic 接口",
        formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--config", default=CONFIG_PATH, help="配置文件路径")
    ap.add_argument("--host", help="监听地址，默认 127.0.0.1")
    ap.add_argument("--port", type=int, help="监听端口，默认 8788")
    ap.add_argument("--new-key", action="store_true", help="重新生成本地 API Key")
    ap.add_argument("--set-token", metavar="TOKEN", help="保存 WorkBuddy 凭据（Bearer token）")
    ap.add_argument("--login", action="store_true",
                    help="★ 走官方登录流程拿 token（不需要会员/API Key）")
    ap.add_argument("--no-browser", action="store_true", help="--login 时不自动打开浏览器")
    ap.add_argument("--login-timeout", type=int, default=300, help="--login 等待秒数，默认 300")
    ap.add_argument("--refresh", action="store_true", help="用 refresh_token 手动续期")
    ap.add_argument("--no-auto-login", action="store_true",
                    help="启动时不自动进入登录流程（默认缺凭据会自动 --login）")
    ap.add_argument("--set-api-key", metavar="KEY", help="保存 WorkBuddy API Key")
    ap.add_argument("--set-helper", metavar="SCRIPT", help="保存 apiKeyHelper 脚本（stdout 即凭据）")
    ap.add_argument("--set-endpoint", metavar="URL", help="自定义后端地址")
    ap.add_argument("--list", action="store_true", help="列出官方模型")
    ap.add_argument("--where", action="store_true", help="显示凭据来自哪一层")
    ap.add_argument("--headers", action="store_true",
                    help="打印实际发给上游的请求头（凭据脱敏）")
    ap.add_argument("--check", action="store_true", help="体检：真发一次请求验证凭据")
    ap.add_argument("--show-config", action="store_true", help="打印配置（凭据脱敏）")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印请求日志")
    ap.add_argument("--dump", action="store_true", help="把每个请求体落盘到 run\\dumps\\（抓包排查）")
    args = ap.parse_args()

    _line_buffer()
    cfg = load_config(args.config)
    registry = ModelRegistry(cfg)
    creds = Credentials(cfg, args.config)
    upstream = Upstream(cfg, registry, creds)

    if args.login:
        return cmd_login(cfg, args.config, no_browser=args.no_browser,
                         timeout=args.login_timeout)
    if args.refresh:
        return cmd_refresh(cfg, args.config)
    if args.set_token:
        cfg.setdefault("upstream", {})["auth_token"] = clean_bearer(args.set_token)
        # auth_token 和 api_key 是两条互斥的凭据路线；设置前者时清空后者，
        # 避免 Credentials.resolve() 之后又把残留 API key 附到 X-API-Key。
        cfg["upstream"]["api_key"] = ""
        save_config(cfg, args.config)
        print("已保存 auth_token: %s" % mask(cfg["upstream"]["auth_token"]))
        print("下一步：python wb_proxy.py --check")
        return 0
    if args.set_api_key:
        cfg.setdefault("upstream", {})["api_key"] = clean_bearer(args.set_api_key)
        save_config(cfg, args.config)
        print("已保存 api_key: %s" % mask(cfg["upstream"]["api_key"]))
        print("下一步：python wb_proxy.py --check")
        return 0
    if args.set_helper:
        cfg.setdefault("upstream", {})["api_key_helper"] = args.set_helper.strip()
        save_config(cfg, args.config)
        print("已保存 api_key_helper: %s" % cfg["upstream"]["api_key_helper"])
        return 0
    if args.set_endpoint:
        cfg.setdefault("upstream", {})["endpoint"] = args.set_endpoint.strip().rstrip("/")
        save_config(cfg, args.config)
        print("已保存 endpoint: %s" % cfg["upstream"]["endpoint"])
        return 0
    if args.new_key:
        cfg["api_key"] = gen_key()
        save_config(cfg, args.config)
        print("新的本地 API Key: %s" % cfg["api_key"])
        return 0
    if args.show_config:
        shown = copy.deepcopy(cfg)
        up = shown.get("upstream") or {}
        # refresh_token 也是完整凭据（能换出新的 access_token），必须一起脱敏
        for k in ("auth_token", "refresh_token", "api_key"):
            if up.get(k):
                up[k] = mask(up[k])
        if shown.get("api_key"):
            shown["api_key"] = mask(shown["api_key"])
        print(json.dumps(shown, ensure_ascii=False, indent=2))
        return 0
    if args.list:
        return cmd_list(registry, upstream)
    if args.where:
        return cmd_where(upstream)
    if args.headers:
        return cmd_headers(cfg, upstream)
    if args.check:
        return cmd_check(cfg, registry, upstream)

    if args.dump:
        cfg["dump_dir"] = os.path.join(SCRIPT_DIR, "run", "dumps")
        print("  [抓包] 每个请求体将落盘到 %s" % cfg["dump_dir"])
    if args.host:
        cfg["host"] = args.host
    if args.port:
        cfg["port"] = args.port

    registry.refresh(force=True)

    # 没配凭据就自动走一次登录流程（--no-auto-login 可跳过）。
    # 这是为了避免"登录完了以为代理也起了，结果 Claude Code 一直 Connection refused"。
    if not args.no_auto_login and not upstream.creds.ready()[0]:
        print("  [i] 还没有凭据，自动进入官方登录流程（--no-auto-login 可跳过）...\n")
        if cmd_login(cfg, args.config, no_browser=args.no_browser,
                     timeout=args.login_timeout) != 0:
            print("\n[!] 登录没完成。没有凭据时，只有自定义模型能用（本代理不代理自定义模型）。")
            print("    仍要强行启动的话加 --no-auto-login。\n")
            return 1
        registry.refresh(force=True)

    port = int(cfg["port"])
    try:
        server = Server((cfg["host"], port), Handler, cfg, registry, upstream,
                        verbose=args.verbose)
    except OSError as e:
        print("启动失败：端口 %d 可能被占用（%s）。用 --port 换一个。" % (port, e))
        return 1

    registry.refresh(force=True)
    print_banner(cfg, registry, upstream)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print()
    finally:
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        log("已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
