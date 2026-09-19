# wb-model-proxy —— 把 WorkBuddy 官方模型接给 Claude Code / Codex

一个**单文件、纯标准库**的本地代理：把 **WorkBuddy 官方提供的那批模型**（hy3 / glm-5.3 /
kimi-k2.7 / deepseek-v4-pro / minimax-m3 …）重新暴露成标准的 **OpenAI + Anthropic 双协议**接口，
这样 Claude Code、Codex CLI 以及任何支持这两套协议的客户端都能直接用。

> **关于"网页版模型"**：WorkBuddy 网页版（`workbuddy.cn/app`）和桌面端是**同一个账号、同一个上游**
> （`copilot.tencent.com`），所以网页版能看到的官方模型，本代理同样覆盖 —— `--login` 拿的就是这个账号的登录态。

> **只管官方模型。** 你自己在 WorkBuddy 里加的自定义模型（带 url + apiKey 的那种）不归它管 ——
> 那种你手里已经有 key，自己直连就行，中间再套一层是多余的。带 `url`、带 `custom-local:` 前缀、
> 或 `vendor=Custom` 的条目会被**自动跳过**（`--list` 会告诉你跳过了几个）。

```
┌──────────────┐        ┌──────────────────────┐        ┌───────────────────────────────┐
│ Claude Code  │        │    wb_proxy.py       │        │  copilot.tencent.com          │
│ Codex CLI    │ ─────► │  :8788               │ ─────► │  /v2/chat/completions         │
│ 任意 SDK      │        │  OpenAI + Anthropic  │        │  （WorkBuddy 官方模型服务）     │
└──────────────┘        │  协议/流式双向转换     │        └───────────────────────────────┘
                        └──────────────────────┘
```

---

## 两步跑起来（不需要会员、不需要 API Key）

**第 1 步：让代理自己走一次官方登录，把 token 拿回来**

```bat
cd C://Users//21022//Desktop//Mark//wb-model-proxy
python wb_proxy.py --login
```

它会打印一个授权地址并自动打开浏览器 —— **用你平时登录 WorkBuddy 的那个账号登录**就行
（免费账号也可以）。地址是 `www.workbuddy.cn/login/?platform=workbuddy&state=…&version=…&loginSessionId=…`，
和桌面端 App 用的**完全同一个登录页**。登录完成后你什么都不用做，脚本会自动把 `access_token` / `refresh_token`
取回来写进 `config.json`：

```
1) 已申请到登录态 state=23bb64a3-3993-4f22-9…
2) 用浏览器打开下面这个地址，用你平时登录 WorkBuddy 的同一个账号登录：
   https://copilot.tencent.com/login?platform=workbuddy&state=23bb64a3-…
3) 登录完成后不用做别的，本脚本会自动把 token 取回来…
4) 登录成功！
   auth_token    eyJhbGciOi…
   refresh_token 8f3c1a…
   已写入 config.json
```

**第 2 步：验证并启动**

```bat
python wb_proxy.py --check     # 拿一个官方模型真发一次请求，通过就是绿的
start.bat
\\本人亲测   
\\cmd管理员权限进入
\\输入   
.\start.bat  
\\可以直接跑起来无闪退
```

> **token 会自动续期**：`access_token` 有效期不长，但代理存了 `refresh_token`，
> 请求遇到 401/403 会自动换新的再重试一次，并把新 token 写回 `config.json` ——
> 基本不用你管。真要手动续：`python wb_proxy.py --refresh`；
> `refresh_token` 也过期了就再 `--login` 一次。
>
> 代理还支持**凭据热加载**：改了 `config.json` 不用重启，下一次请求就用新值。

### 有会员的话，也可以直接上官方 API Key

| 版本 | 拿 API Key 的地址 |
|---|---|
| 中国版 | https://copilot.tencent.com/profile/ |
| 海外版 | https://www.codebuddy.ai/profile/keys |

```bat
python wb_proxy.py --set-api-key "sk-你复制到的key"
python wb_proxy.py --check
```

### 启动

```bat
start.bat
```

启动后：

```
  OpenAI 兼容   http://127.0.0.1:8788/v1     (/chat/completions)
  Anthropic     http://127.0.0.1:8788/v1     (/messages  ← Claude Code 用这个)
  API Key       sk-wb-xxxxxxxxxxxxxxxx
  官方模型      60 个（跳过自定义/外部 1 个）
  上游          https://copilot.tencent.com/v2/chat/completions
  凭据          config.json → upstream.api_key
```

**客户端只填两样**：`Base URL = http://127.0.0.1:8788/v1`，`API Key = config.json 里的 api_key`。

---

## 关于凭据（重点）

**不需要会员。** `--login` 走的就是 WorkBuddy 自己的 SSO 登录流程（和桌面端登录同一个），
所以免费账号一样能拿到 token、一样能用这套接口。

**我也不会去抠软件里的凭据**。排查过了：软件自己那份登录态是真的加密了 ——

| 查过的地方 | 结果 |
|---|---|
| CLI 进程环境变量（`/api/v1/envs` 暴露的全部 157 个） | 没有任何 token |
| `~/.workbuddy/settings.json`、`~/.codebuddy/settings.json` | 只有插件/沙箱开关 |
| 应用外溢给 CLI 的产品配置 | `authentication` 只有鉴权方式元数据，没 token |
| `local_storage` / LevelDB / `workbuddy.db` | 值都过了 safeStorage + Windows DPAPI 加密 |
| PAC RPC 命名管道、本地 55243 的 `/api/v1/auth/*` | 前者是解析代理，后者只回 `authenticated:true` |

所以我选了**更干净的路**：不去解密它的存储，而是**复用它的登录流程**自己去换一个 token。
这样既不需要会员、不碰任何加密数据，还能自动续期。

## 接客户端

### Claude Code

```powershell
$env:ANTHROPIC_BASE_URL    = "http://127.0.0.1:8788"    # 带 /v1 也能用（代理会自动归一化）
$env:ANTHROPIC_AUTH_TOKEN  = "sk-wb-你的key"
$env:ANTHROPIC_MODEL       = "deepseek-v4-pro"          # 换成 /v1/models 里任意模型
$env:ANTHROPIC_SMALL_FAST_MODEL = "hy3"
claude
```

### Codex CLI（`~/.codex/config.toml`）

> **2026-09-13：这份配置已经写好了**（`wb` provider 已加、默认已切到 `wb` + `kimi-k2.7`；
> 原来的 `custom` provider 原样保留，想切回去把 `model_provider` 改回 `"custom"` 即可）。
> 你只需要设 `WB_PROXY_KEY` 这一个环境变量就能跑。注意当前 config.toml 里
> `wire_api` 用的是 `"chat"`（本代理没有实现 Codex 的 `responses` 接口）。

```toml
model = "kimi-k2.7"
model_provider = "wb"

[model_providers.wb]
name = "WorkBuddy Model Proxy"
base_url = "http://127.0.0.1:8788/v1"
wire_api = "chat"
env_key = "WB_PROXY_KEY"
```

```powershell
$env:WB_PROXY_KEY = "sk-wb-你的key"
codex
```

### OpenAI SDK

```python
from openai import OpenAI
c = OpenAI(base_url="http://127.0.0.1:8788/v1", api_key="sk-wb-你的key")
r = c.chat.completions.create(model="hy3", messages=[{"role": "user", "content": "你好"}])
print(r.choices[0].message.content)
```

---

## 凭据怎么给（六层，按优先级，全都不用改代码）

官方 CLI 的优先级是 `CODEBUDDY_AUTH_TOKEN > apiKeyHelper > CODEBUDDY_API_KEY`，本代理一致。

| 优先级 | 来源 | 怎么给 |
|---|---|---|
| 1 | `config.json` → `upstream.auth_token` | `python wb_proxy.py --set-token "<token>"` |
| 2 | `config.json` → `upstream.api_key` | `python wb_proxy.py --set-api-key "<key>"` ← **推荐** |
| 3 | 环境变量 `CODEBUDDY_AUTH_TOKEN` / `CODEBUDDY_API_KEY` | 和 WorkBuddy CLI 同名，可直接复用 |
| 4 | `~/.workbuddy/settings.json` 的 `env.*` | 官方 CLI 也读这里；本代理只读，不改你的文件 |
| 5 | `settings.json` 的 `apiKeyHelper` | 一个能打印出 key 的脚本，stdout 即凭据 |
| 6 | `config.json` → `upstream.api_key_helper` | 同上，路径写在本代理自己的配置里 |

随时看当前凭据来自哪一层：

```bat
python wb_proxy.py --where
```

**改凭据不用重启代理**：代理按文件 mtime 热加载 `config.json`，所以你 `--set-token` /
`--set-api-key` 写完，**运行中的进程下一次请求就用新值**（token 过期时特别省事）。

用官方 API Key 时，**中国版还要设 `CODEBUDDY_INTERNET_ENVIRONMENT=internal`** ——
官方文档特别强调这是最常漏的一项（不设会认证失败或连错端点）。本代理请求头里的
`x-default-env` 也跟着这个变量走，默认 `internal`。

---

## 模型清单

启动时从 WorkBuddy 自己的配置里读，不用手抄：

**模型清单 = 所有来源的并集**（按 id 去重）。为什么要合并：App 会定期重写缓存/外溢文件，
某一瞬间某个文件可能只有 3 条残缺记录（实测踩过：官方模型从 51 个掉到 3 个），只认单一来源
会跟着残缺。合并后实测 **60 个官方模型**（2026-09-13 核对）。

| 来源 | 说明 |
|---|---|
| `%ACC_PRODUCT_CONFIG_PATH%`（App 外溢到 TEMP 的当前配置） | 最"新鲜" |
| `~/.workbuddy/cache/acc-product-config-v3.json`（缓存） | App 定期重写，可能瞬间残缺 |
| `<WorkBuddy>\resources\app.asar.unpacked\cli\product.json` | CLI 自带的静态全量清单（48 个），**永远存在**，兜底 |
| `<WorkBuddy>\resources\app.asar.unpacked\cli\product.internal.json` | CLI 内部清单（46 个） |

只保留「官方提供」的条目；下面这些**跳过**：带 `url`（自带外部地址）、
带 `custom-local:` / `custom:` 前缀、`vendor` 为 `Custom`/`Enterprise`。

```bat
python wb_proxy.py --list
```

实测清单（2026-09-13，共 60 个，全量以 `--list` 为准）：

- **主力对话 / 代码**：`hy3` / `hy3-x` / `hy3-preview` / `hy4-preview`、
  `glm-5.3` / `glm-5.3-flash` / `glm-5.2` / `glm-5.1` / `glm-5.0-turbo` / `glm-5v-turbo`、
  `kimi-k3-1` / `kimi-k2.8-preview` / `kimi-k2.7`（Kimi-K2.7-Code）/ `kimi-k2-thinking`、
  `minimax-m3` / `minimax-m2.7`、
  `deepseek-v4-pro` / `deepseek-v4-flash` / `deepseek-v4.1-flash` / `deepseek-v3-2-volc` / `deepseek-r1-0528`
- **Claude 系列名**：`default-1.1`（Claude-3.7-Sonnet）/ `default-1.2`（Claude-4.0-Sonnet）/ `default`
- **混元系**：`hunyuan-2.0-instruct` / `hunyuan-2.0-thinking` / `hunyuan-chat`（Hunyuan-Turbos）/ `hunyuan-3b` / `hunyuan-7b-dense`
- **路由类**：`fast-model` / `balanced-model` / `deep-model` / `auto`
- **补全 / 内部类**：`completion-gf` / `codewise-*` 等
- **图像 / 视频**：`hunyuan-image-*` / `kling-v3-t2v` / `kling-v3-i2v`（对话接口调不了，仅供 `--list` 参考）

---

## 命令一览

```bat
python wb_proxy.py                        # 启动（默认 127.0.0.1:8788）
python wb_proxy.py --list                 # 列出官方模型 + 跳过统计
python wb_proxy.py --where                # 凭据来自哪一层
python wb_proxy.py --headers              # 打印实际发给上游的请求头（凭据脱敏）
python wb_proxy.py --check                # 体检：拿官方模型真发一次请求
python wb_proxy.py --set-api-key "..."    # 保存官方 API Key（推荐）
python wb_proxy.py --set-token "..."      # 保存 Bearer token
python wb_proxy.py --set-helper script    # 用脚本动态产出 key
python wb_proxy.py --set-endpoint URL     # 换后端地址（默认 copilot.tencent.com）
python wb_proxy.py --port 9000
python wb_proxy.py --new-key              # 重新生成本地 API Key
python wb_proxy.py --show-config          # 打印配置（凭据脱敏）
python wb_proxy.py -v                     # 打印请求日志
python wb_proxy.py --dump                 # 抓包：把每个请求体落盘到 run\\dumps\\（排查偶发问题用）

python wb_proxy.py --login                # ★ 官方登录拿 token（不需要会员）
python wb_proxy.py --refresh              # 手动续期
python test_wb_proxy.py                   # 离线自测 98 项，不联网、不需要凭据
python smoke_live.py                      # 真机冒烟（会消耗额度）
```

批处理：`start.bat` / **`start-bg.bat`（后台常驻）** / **`stop-bg.bat`（停止）** / `check.bat` / `selftest.bat`

> **代理必须一直挂着**，一关 Claude Code / Codex 就报 `Connection refused`。
> 用 `start-bg.bat` 会在一个最小化的独立窗口里常驻运行，不占你当前终端也不容易误关；
> 停止用 `stop-bg.bat`。

---

## config.json

| 键 | 默认 | 说明 |
|---|---|---|
| `host` / `port` | `127.0.0.1` / `8788` | 监听地址 |
| `api_key` | 自动生成 | **客户端只认这一把** |
| `allow_any_key` | `false` | `true` 则完全不校验（仅本机调试） |
| `default_model` | 空（取清单第一个） | 请求里的模型名认不出来时用它兜底 |
| `upstream.endpoint` / `path` | `https://copilot.tencent.com` / `/v2/chat/completions` | 官方模型上游 |
| `upstream.auth_token` / `refresh_token` | 空 | `--login` 自动写入，遇 401 自动用 refresh 续期 |
| `upstream.api_key` | 空 | 有会员才需要，`--set-api-key` 写入 |
| `upstream.api_key_helper` | 空 | 产出 key 的脚本 |
| `upstream.user_id` / `session_id` | 空 | 想带 `X-User-Id` / `X-WorkBuddy-Session-Id` 时填 |
| `upstream.extra_headers` | `{}` | 额外请求头 |
| `max_upstream_concurrency` | `2` | **同时打到上游的最大请求数**。Claude Code 一轮会并行发主请求+后台请求，并发太高会被上游直接重置连接（WinError 10054）或风控（11128）；调大并发=更快但更易被拦 |
| `transient_retry_waits` | `[2, 6, 15, 30, 60]` | **被 11128 拦截时的退避重试间隔（秒）**，列表长度 = 最多重试几次（首次尝试不计）。默认 6 次尝试、最长等 113s —— 因为实测拦截窗口能超过 23s，短退避必废。嫌拖慢对话就调小，想更耐拦就加长 |
| `dump_on_11128` | `true` | 被 11128 拦时**自动把该次请求体落盘**（只落第一次），用于留现场 |
| `dump_11128_dir` | 空（= `run\dumps`） | 上面那个现场落盘的目录 |
| `read_timeout` | 600 | 上游读超时（思考模型慢，别调小） |
| `models_cache_ttl` | 60 | 模型清单缓存秒数 |
| `user_agent` | 空（= `wb-model-proxy/2.0 (+local)`） | 发给上游的 `User-Agent`。留空即如实标识本代理；想自定义写这里，或写 `upstream.extra_headers`（优先级更高）。**别用来伪装官方 CLI** —— 见下节「关于请求指纹」 |

---

## 协议转换

上游是 OpenAI 协议，所以 **Anthropic 这一侧由本代理双向转换**。

> ⚠️ **上游只接受流式请求**（非流式会返回 `400 code=11101 Non-stream chat request is
> currently not supported`）。所以代理对外一发请求就**一律带 `stream: true`**；
> 客户端要非流式时，由代理把 SSE 增量自己拼回完整的 `chat.completion`
> （正文、`reasoning_content`、`tool_calls` 的 arguments 都要按分片归并），
> 再按对应协议返回。所以**非流式也能用，只是内部走的是流式**。

协议映射表：

| Anthropic | ↔ | OpenAI |
|---|---|---|
| `system`（字符串/块） | → | `messages[0]`（role=system） |
| `content: [{type:tool_use}]` | ↔ | `tool_calls` |
| `content: [{type:tool_result}]` | ↔ | `role: tool` + `tool_call_id` |
| `tools[].input_schema` | ↔ | `tools[].function.parameters` |
| `tool_choice: auto/any/none/tool` | ↔ | `auto/required/none/{function}` |
| `stop_reason: end_turn/max_tokens/tool_use` | ↔ | `finish_reason: stop/length/tool_calls` |
| `thinking` 块 | ← | `reasoning_content` |

流式会完整产出 Anthropic 官方事件序列：
`message_start → ping → (content_block_start → *_delta → content_block_stop)* → message_delta → message_stop`，
且**不泄漏 `[DONE]`**（Anthropic 客户端不认）。模型的思考过程走 `reasoning_content`（OpenAI 侧）
/ `thinking` 块（Anthropic 侧），不会丢。

---

## 关于请求指纹（为什么不伪装官方客户端）

排查 11128 时把官方 CLI 挖了一遍：它**确实**带很多标识头，但要看清它们各属于哪条调用链 ——
这些头散在**三套互相独立的子系统**里，没有一条证据指向模型补全接口：

| 头 | 实际出处 | 用在哪 |
|---|---|---|
| `User-Agent: CodeBuddyCode/1.0` | IDE 连接层 | 连 IDE 的 **WebSocket**（MCP 握手），不是模型请求 |
| `X-Device-Id`（= `machineId`） | `SandboxAudit` 遥测 | 沙箱审计上报，不是模型请求 |
| `X-Stainless-Lang/OS/Arch/Runtime/Package-Version` | 内置的 **Stainless/OpenAI Node SDK** 自动加 | 走该 SDK 的请求；是 SDK 的运行时自述，不是产品指纹 |
| `X-Conversation-ID` / `X-Session-ID` / `X-Trace-ID` / `X-Agent-Type` / `X-IDE-*` / `X-Product-Version` / `X-Data-Tag` | 会话 / hook / service-proxy 接口 | 会话与 IDE 集成链路，不是 `/v2/chat/completions` |

所以「补齐这些头就更像官方客户端」这个前提**站不住**：它们本来就不是模型请求的组成部分，
硬拼过来只会拼出个四不像（比如给一个 Python 客户端贴 `X-Stainless-Runtime: node`）。

**本代理的做法：如实标识自己。** 之前是**一个 UA 都不带**（`http.client` 不会自动加），
正常 HTTP 客户端不该匿名，所以补上了 `User-Agent: wb-model-proxy/2.0 (+local)`。

想自己看、自己改：

```bat
python wb_proxy.py --headers     # 打印实际会发出去的头（凭据脱敏）
```

```json
{ "user_agent": "my-own-agent/1.0",
  "upstream": { "extra_headers": { "X-Whatever": "1" } } }
```

两者都**热加载**，改完下一次请求就生效（`extra_headers` 优先级最高，能覆盖任何内置头）。

**不做的事**：不伪造官方客户端的 UA / 指纹去「伪装渠道」。两个理由，都不是道德说教 ——
① **没用**：11128 实测与请求头无关（排错表那一行里，体积 / 内容 / 工具 / `max_tokens` / 并发
全都用控制变量排除过，复用同一把 token 连打 30+ 次全 200）；
② **更容易被挑出来**：一个自称官方、细节却对不上的指纹，比一个诚实自述的本地代理，
更容易被风控判成异常。

## 排错

| 现象 | 处理 |
|---|---|
| **`Connection refused`** | **代理没在跑**（90% 是这个，`--login` 成功 ≠ 代理已启动）。`start-bg.bat` 常驻启动；自查 `curl http://127.0.0.1:8788/health` |
| 日志里出现大段 `ConnectionResetError` traceback | **无害**。那是 Claude Code 请求完就断开 keep-alive 连接，服务端已在 `handle_error` 里静音；若还能看到说明用的是旧文件 |
| 某次会话**持续** 11128 | 大概率是那次会话**累积的上下文**触发了过滤（每次发消息都会带上全部历史）。\n  先试 `/clear` 开新会话再说「你好」；新会话通了就说明是旧会话的内容问题 |
| 503 `credential_missing` | 还没配凭据 → 先 `python wb_proxy.py --login` |
| 401 / 403 | token 过期。有 `refresh_token` 会自动续期重试；还不行就重新 `--login` |
| 504 `API key verification service unavailable` | 走的是 API Key 且后端不认。改用 `--login` 最省事 |
| Claude Code 提示 `xxx is not a model this version of Claude Code recognizes` | 只是它不认识这个模型名的上下文窗口，不影响使用。想消掉就设
  `CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1`，或给模型名加 `[1m]` 后缀 |
| `429 code=6004` 换个模型也不行 | 那是**账号级**限频，等一会儿；单个模型的额度是按模型分开算的，换模型通常能解 |
| `--login` 一直等不到 | 确认浏览器里**登录成功**了（免费账号也行）；`--login-timeout` 调大 |
| 登录页打不开 / 域名不对 | 用的是 `www.workbuddy.cn/login/`（App 同款）；可改 `upstream.login_page` 模板 |
| `429 code=6004` 「使用量已超出频率限制，将于 … 重置」 | **这个模型当天的额度用完了**，换一个模型（`--list` 看清单）；到提示的时间自动恢复 |
| 回复是空的 / 只有 thinking 没有正文 | 思考模型把 token 都花在思考上了，`max_tokens` 给 400~600 以上 |
| `400 code=11101 Non-stream ... not supported` | 上游只收流式。当前版本已强制走流式；若出现说明 `open_stream` 里的 `stream=True` 被去掉了 |
| `400 code=11128 Illegal API invocation from an unapproved channel` | **上游安全策略的偶发拦截**，不是你本地或代理的问题。**触发条件至今没能稳定复现** —— 下面这些解释都被控制变量排除过：请求体体积（8B~32KB）、内容里的 Claude Code 品牌词、tools / `max_tokens`（8~4096）、串行 / 6 路并发、连续 60 次的高频量（60/60 全 200）。它的特征是**成串**出现（连续 3~4 次被拦、跨度 > 23s），窗口过去立刻恢复。代理替客户端做退避重试，默认 6 次尝试 / 最长 113s（`transient_retry_waits`）；被拦时还会把现场请求体自动落盘到 `run\dumps\11128-*.json` —— 下次再被拦，把这个文件发我就能精确定位。 |
| `502 上游连接被重置（WinError 10054）` | 并发太高被上游掐了连接。代理已自动重试 2 次；还会出现就把 `max_upstream_concurrency` 调成 `1` |
| `502 upstream connect failed（WinError 10061）` | **本机到上游的出网被拒**（不是代理代码问题）。2026-09-13 实测：开启 VPN/TUN 或系统代理半开（客户端退了但虚拟网卡/系统代理还挂着）时会出现。排查：① 自己的 cmd 里 `curl -I https://copilot.tencent.com` 看是否同样被拒；② 关掉/重启 VPN 客户端、检查系统代理设置；③ 恢复后代理**不用重启**，下一次请求自动恢复 |
| `429 code=6004` 某个模型一直 429 | 那个模型当天额度用完，换模型或等重置时间（按模型分别计额） |
| Claude Code 卡 waiting for API | 先 `curl http://127.0.0.1:8788/health` 确认代理在；模型名用 `--list` 核对 |
| 一个官方模型都没读到 | `--list` 看来源路径；确认 WorkBuddy 已安装且登录过 |
| 登录后仍报未授权 | `python wb_proxy.py --where` 看凭据来源，确认没被环境变量里的旧 token 抢占 |
| 端口被占用 | `--port` 换一个 |
| 想确认请求打到了哪 | 响应头 `X-Proxy-Scope` / `X-Proxy-Model`，或启动时加 `-v` |
| 客户端把 BASE_URL 写成了 `.../v1` | **现在也能用**（代理会把 `/v1/v1/*` 归一化成 `/v1/*`）；但建议还是写不带 /v1 的标准形式 |
| 偶发 11128 想抓真实请求 | **现在自动抓了** —— 被拦时现场请求体直接落盘到 `run\dumps\11128-*.json`（`dump_on_11128` 默认开），把它发我即可精确定位。想抓**全部**请求另开 `python wb_proxy.py --dump` |

## 会不会在 WorkBuddy 里留下对话记录？

**不会。** `/v2/chat/completions` 是**无状态**的补全接口：

- 请求里没有任何会话标识（本代理也不发 `X-WorkBuddy-Session-Id`）；
- 响应里只有 `traceid` / `x-request-id` / `eo-log-uuid` 这类**单次请求**的追踪 id，没有 conversation id，也没有任何「会话已创建」的迹象。

所以关掉代理也不需要去删什么会话 —— 服务端本来就没存。会留下的只有**积分消耗流水**（这个删不掉，但只是记账）。

**如果你指的是 Claude Code 自己的本地会话**（`~/.claude/projects/` 下的一堆 `.jsonl`），那是 Claude Code 自己存的，跟本代理无关：会话内 `/clear` 清当前，或直接删对应 jsonl 文件。

---

## 边界与注意

- 用的是你 WorkBuddy 账号的权益，别批量刷量、别转售。
- 接口形态（`/v2/chat/completions`、请求头）可能随 WorkBuddy 版本变化。失效时用 `--check` 定位，
  改 `config.json` 的 `upstream.path` / `extra_headers` 即可，代码不用动。
- 本代理只带必要的鉴权与模型请求字段，不转发位置、遥测等无关头。
- 仅供个人本地使用：别把 8788 暴露到公网。

---

## 文件说明

| 文件 | 作用 |
|---|---|
| `wb_proxy.py` | 代理本体（纯标准库，零依赖） |
| `config.json` | 配置（首次启动自动生成 api_key） |
| `test_wb_proxy.py` | 离线自测 **98 项**：官方模型筛选、凭据六层、双协议双向转换、流式事件序列、错误处理 |
| `smoke_live.py` | 真机冒烟：对已启动的代理打真实请求（双协议 × 流式/非流式） |
| `start.bat` / `check.bat` / `selftest.bat` | Windows 一键脚本 |
