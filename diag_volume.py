# -*- coding: utf-8 -*-
"""持续量测试：连发 60 次（不等间隔），看 11128 是不是"量"触发。
只发 max_tokens=8，60 次总消耗可忽略。"""
import io
import json
import sys
import time

sys.path.insert(0, r"C:\Users\21022\Desktop\Mark\wb-model-proxy")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import wb_proxy as w  # noqa: E402

MODEL = "hy3"
N = 60

cfg = w.load_config()
up = w.Upstream(cfg, None, w.Credentials(cfg, w.CONFIG_PATH))
print("上游 = %s   模型 = %s   目标 = 连发 %d 次\n" % (up.endpoint(), MODEL, N), flush=True)

t0 = time.time()
hits = []
for i in range(1, N + 1):
    body = {"model": MODEL, "stream": True, "max_tokens": 8,
            "messages": [{"role": "user", "content": "1"}]}
    try:
        conn, resp = up._open_once(body)
    except Exception as e:
        print("  #%-3d %6.1fs  连接异常: %s" % (i, time.time() - t0, str(e)[:70]), flush=True)
        time.sleep(0.5)
        continue
    code = 200
    if resp.status == 200:
        try:
            resp.read(64)
        except Exception:
            pass
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
    tag = "过"
    if code != 200:
        tag = ("**11128**" if code == 11128 else "其它(%s)" % code)
        hits.append((i, code, round(time.time() - t0, 1)))
    print("  #%-3d %6.1fs  %s" % (i, time.time() - t0, tag), flush=True)

print("\n=== 汇总 ===")
print("  总次数 %d，拦截 %d 次，总耗时 %.1fs" % (N, len(hits), time.time() - t0))
if hits:
    print("  拦截位置: " + ", ".join("#%d(%.1fs,code=%s)" % h for h in hits))
else:
    print("  全程无拦截")
