# -*- coding: utf-8 -*-
"""
自动打卡 v1.4 · OAuth2 自动登录 · 限流自动重试
=====================================================================
OAuth2 Bearer Token 认证。
脚本会自动取得 access_token 并在每个请求上带 Authorization: Bearer。



    python punch.py                 ->  打完全部 5 种卡
    python punch.py --dry-run       ->  只打印请求，不上传不提交
    python punch.py --check         ->  检查登录态是否有效
    python punch.py -t 学习,运动     ->  临时只打指定类型
    python punch.py -i "D:\\照片"    ->  临时换图片目录


"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("缺少依赖，请先执行：  pip install requests")

# ══════════════════════ 配置区 ══════════════════════

# ---- 登录----
# A refresh_token：长期有效，脚本自动换新 access_token 并滚动保存到缓存，
#   来自登录抓包 /connect/token 响应。
REFRESH_TOKEN = ""

# B 账号密码登录。
USERNAME = ""
PASSWORD = ""
LOGIN_CODE = ""

# C access_token：抓包/connect/token 返回的 access_token，有效期 5 小时，
#   过期后脚本会自动改用上面的 REFRESH_TOKEN 续期。
ACCESS_TOKEN = ""

# 登录接口的应用参数
CLIENT_ID = "43215cdff2d5407f8af074d2d7e589ee"
CLIENT_SECRET = "DBqEL1YfBmKgT9O491J1YnYoq84lYtB/LwMabAS2JEqa8I+r3z1VrDqymjisqJn3"
TENANT_ID = "32"

# token 缓存文件
TOKEN_CACHE = "logs/token_cache.json"

COOKIE = ""

BASE_URL = "https://ai.cqzuxia.com"

# 图片路径：可以是「图片目录」或「单个图片文件」，可以写多个。
# 传目录时递归扫描并随机挑图。
# 平台
# Windows
# IMAGE_DIRS = [r"E:\图片\打卡"]
# Linux
IMAGE_DIRS = ["~/pictures/打卡"]
# Android / Termux（需先授权）
# IMAGE_DIRS = ["~/storage/emulated/0/Pictures/打卡"]

# 每次打卡随机插入几张图片
IMAGE_COUNT = 1

# 每项打卡之间的间隔秒数（自动带随机抖动，0 表示不等待）
# 服务端有速率限制：1 秒间隔会返回「超出速率限制」
DELAY = 10.0

# 命中「超出速率限制」时自动等待并重试
RATE_LIMIT_WAIT = 30       # 首次等待秒数
RATE_LIMIT_RETRIES = 3     # 最多自动重试次数

# 当天已打过同类「每日任务」时是否跳过（风采展示不受影响）
SKIP_IF_DONE = False

# True = 不提交；无误后改回 False 才会打卡
DRY_RUN = False

# 日志文件； None 可关闭
LOG_FILE = "logs/punch.log"

#   打卡任务：
#   comment = 输入的文字内容      tag = 风采展示额外选的主题
#   images  = 给这一种单独指定图片路径（None = 用上面的 IMAGE_DIRS）
TASKS = [
    dict(name="学习",    endpoint="daily",   asstype="1", comment="学习", tag=None,   need_image=False, images=None),
    dict(name="运动",    endpoint="daily",   asstype="0", comment="运动", tag=None,   need_image=True,  images=None),
    dict(name="风-健康", endpoint="elevate", asstype="0", comment="运动", tag="其它", need_image=True,  images=None),
    dict(name="风-思维", endpoint="elevate", asstype="1", comment="学习", tag="其他", need_image=True,  images=None),
    dict(name="风-技术", endpoint="elevate", asstype="2", comment="学习", tag="其他", need_image=True,  images=None),
]

# ══════════════════════ 配置区结束 ══════════════════════

TOKEN_PATH = "/connect/token"
UPLOAD_PATH = "/oss/api/SmartFiles/UpLoadFormFile"
DAILY_PATH = "/growing/api/StuTask/SaveStuDailyAssignment"
ELEVATE_PATH = "/growing/api/StuTask/SaveStuElevateAssignment"
LIST_PATH = "/growing/api/StuTask/GetStuDailyList"
BUCKET = "stu-growing"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0")
IMAGE_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif",
}
MAX_RETRIES = 3
TIMEOUT = 20
RATE_LIMIT_HINTS = ("速率限制", "频率过快", "操作频繁", "请求过于频繁", "稍后再试",
                    "rate limit", "too many requests")

AUTH_HELP = """
还没有可用的登录凭据。本站点使用 OAuth2 Bearer Token（不是 Cookie），请任选一种填到配置区：

  A. REFRESH_TOKEN（推荐，长期免登录）
     登录网站时 F12 -> 网络 -> 找到 POST /connect/token -> 响应(Response)
     -> 复制 JSON 里的 refresh_token 值，填到 REFRESH_TOKEN = "" 里。

  B. USERNAME + PASSWORD（站点无需验证码，填这两项即可）

  C. ACCESS_TOKEN（同一响应里的 access_token，5 小时内有效，过期后会自动走 A）

重新抓包时记得勾选「保留日志」，并在点登录之前就开始录制。
"""


class PunchError(Exception):
    """配置 / 图片 / 业务前置条件不满足。"""


class AuthError(PunchError):
    """登录态缺失、过期或换取失败。"""


class ApiError(PunchError):
    """接口返回错误。"""


class RateLimitError(ApiError):
    """服务端速率限制，等待后可重试。"""


# ------------------------- 日志 -------------------------
_LOG_PATH: Path | None = None


def _local_path(name: str) -> Path:
    p = Path(name)
    return p if p.is_absolute() else Path(__file__).resolve().parent / p


def _init_log() -> None:
    global _LOG_PATH
    if LOG_FILE:
        _LOG_PATH = _local_path(LOG_FILE)
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def log(level: str, event: str, **fields) -> None:
    record = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "level": level, "event": event,
    }
    for k, v in fields.items():
        record[k] = "***" if any(s in k.lower() for s in
                                 ("cookie", "token", "password", "secret", "username")) else v
    if _LOG_PATH:
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    extra = " ".join(f"{k}={v}" for k, v in record.items()
                     if k not in ("ts", "level", "event"))
    line = f"[{record['ts'][11:19]}] {level:<5} {event}"
    if extra:
        line += f" | {extra}"
    print(line, file=sys.stderr if level == "ERROR" else sys.stdout, flush=True)


# ------------------------- 图片 -------------------------
def collect_images(paths) -> list[Path]:
    pool: list[Path] = []
    seen: set[str] = set()
    for raw in paths or []:
        p = Path(raw).expanduser()
        if not p.exists():
            raise PunchError(f"图片路径不存在：{p}（请检查配置区 IMAGE_DIRS）")
        candidates = [p] if p.is_file() else sorted(p.rglob("*"))
        for f in candidates:
            if not f.is_file():
                continue
            if f.suffix.lower() not in IMAGE_MIME:
                if p.is_file():
                    raise PunchError(f"不支持的图片格式：{f}")
                continue
            key = str(f.resolve()).lower()
            if key not in seen:
                seen.add(key)
                pool.append(f)
    return pool


def pick_images(pool: list[Path], count: int) -> list[Path]:
    if count <= 0 or not pool:
        return []
    rng = random.SystemRandom()
    return rng.sample(pool, count) if count <= len(pool) else [rng.choice(pool) for _ in range(count)]


def file_part(path: Path) -> tuple[str, bytes, str]:
    data = path.read_bytes()
    if not data:
        raise PunchError(f"图片是空文件：{path}")
    if len(data) > 20 * 1024 * 1024:
        raise PunchError(f"图片超过 20MB：{path}")
    ext = path.suffix.lower()
    return uuid.uuid4().hex + ext, data, IMAGE_MIME[ext]


# ------------------------- HTTP -------------------------
def make_session(cookie: str = "") -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": BASE_URL,
        "Referer": BASE_URL + "/stu-growing/",
    })
    if cookie:
        s.headers["Cookie"] = cookie
    return s


def request(session, method: str, path: str, *, dry: bool, label: str = "",
            json_body=None, data=None, files=None, params=None, echo=None) -> dict:
    """发请求；dry=True 时只打印不发送。5xx / 网络异常最多重试 3 次。"""
    if dry:
        body = echo if echo is not None else (json_body if json_body is not None else params)
        log("INFO", "预演（未发送）", method=method, path=path,
            body=json.dumps(body or {}, ensure_ascii=False)[:600])
        return {"code": 0, "success": True, "data": {}}

    url = BASE_URL + path
    resp = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.request(method, url, json=json_body, data=data,
                                   files=files, params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            log("WARN", "网络异常，准备重试", attempt=attempt, error=type(exc).__name__)
            if attempt < MAX_RETRIES:
                time.sleep(0.8 * 2 ** (attempt - 1))
                continue
            raise ApiError(f"网络请求失败（已重试 {MAX_RETRIES} 次）：{type(exc).__name__}") from exc
        if resp.status_code >= 500 and attempt < MAX_RETRIES:
            log("WARN", "服务端 5xx，准备重试", status=resp.status_code, attempt=attempt)
            time.sleep(0.8 * 2 ** (attempt - 1))
            continue
        break

    if resp is None:
        raise ApiError("请求未发出")
    if resp.status_code == 429:
        raise RateLimitError("HTTP 429 超出速率限制")
    if resp.status_code in (401, 403):
        raise AuthError(f"登录态已失效（HTTP {resp.status_code}），请更新配置区的登录凭据")
    if not resp.ok:
        raise ApiError(f"请求失败：HTTP {resp.status_code} {label}")
    if "json" not in resp.headers.get("Content-Type", "").lower():
        raise AuthError("返回的不是 JSON，通常说明被重定向到登录页，请更新登录凭据")
    try:
        obj = resp.json()
    except ValueError as exc:
        raise ApiError(f"响应不是合法 JSON：{label}") from exc
    if isinstance(obj, dict):
        code = obj.get("code")
        if obj.get("success") is False or (code is not None and code not in (0, "0")):
            msg = obj.get("msg") or obj.get("message") or "接口返回业务错误"
            if code in (401, 403) or "登录" in str(msg) or "未授权" in str(msg):
                raise AuthError(f"{msg}（请更新登录凭据）")
            if any(h in str(msg).lower() for h in RATE_LIMIT_HINTS):
                raise RateLimitError(f"{label} 失败：{msg}")
            raise ApiError(f"{label} 失败：{msg}")
    return obj


# ------------------------- 认证：自动取得 Bearer Token -------------------------
def jwt_exp(token: str) -> int:
    """从 JWT 里读出过期时间（unix 秒），读不出返回 0。"""
    try:
        payload = token.split(".")[1] + "=="
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:
        return 0


def load_cache() -> dict:
    if not TOKEN_CACHE:
        return {}
    try:
        return json.loads(_local_path(TOKEN_CACHE).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(obj: dict) -> None:
    if not TOKEN_CACHE:
        return
    p = _local_path(TOKEN_CACHE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def store_token(obj: dict) -> str:
    access = obj.get("access_token")
    if not access:
        raise AuthError("登录接口未返回 access_token")
    cache = {
        "access_token": access,
        "refresh_token": (obj.get("refresh_token") or load_cache().get("refresh_token")
                          or REFRESH_TOKEN),
        "exp": int(time.time()) + int(obj.get("expires_in") or 18000),
    }
    save_cache(cache)
    return access


def refresh_access_token(session, refresh: str) -> str:
    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    session.headers.pop("Authorization", None)
    try:
        obj = request(session, "POST", TOKEN_PATH, data=form,
                      echo={"grant_type": "refresh_token"}, dry=False, label="refresh_token")
    except (ApiError, AuthError) as exc:
        raise AuthError(f"refresh_token 换发失败（{exc}）；可能已失效，请重新抓一次登录包") from exc
    return store_token(obj)


def login_password(session) -> str:
    if not (USERNAME and PASSWORD):
        raise AuthError("未配置 USERNAME / PASSWORD")
    # 该站点登录无需验证码；保留 code 字段以匹配抓包中的表单结构，值取 LOGIN_CODE（默认空）
    code = LOGIN_CODE
    form = {
        "username": USERNAME,
        "password": PASSWORD,
        "code": code,
        "vid": "",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "password",
        "tenant_id": TENANT_ID,
    }
    session.headers.pop("Authorization", None)
    try:
        obj = request(session, "POST", TOKEN_PATH, data=form,
                      echo={"grant_type": "password"}, dry=False, label="password_login")
    except (ApiError, AuthError) as exc:
        raise AuthError(f"账号密码登录失败（{exc}）；请检查 USERNAME / PASSWORD") from exc
    return store_token(obj)


def resolve_token(session, *, dry: bool, cookie_fallback: bool = False) -> str | None:
    """按 缓存 → ACCESS_TOKEN → refresh_token → 账号密码 的顺序取得 access_token。"""
    if dry:
        return None
    now = time.time()

    cache = load_cache()
    if cache.get("access_token") and cache.get("exp", 0) > now + 60:
        log("INFO", "复用缓存 access_token",
            expire_in_min=round((cache["exp"] - now) / 60))
        return cache["access_token"]

    if ACCESS_TOKEN:
        exp = jwt_exp(ACCESS_TOKEN)
        if exp > now + 60:
            log("INFO", "使用配置区 ACCESS_TOKEN",
                expire_in_min=round((exp - now) / 60))
            return ACCESS_TOKEN
        log("WARN", "配置区 ACCESS_TOKEN 已过期，尝试自动续期")

    refresh = cache.get("refresh_token") or REFRESH_TOKEN or os.environ.get("PUNCH_REFRESH_TOKEN", "")
    if refresh:
        try:
            token = refresh_access_token(session, refresh)
            log("INFO", "refresh_token 换发成功")
            return token
        except AuthError as exc:
            log("WARN", "刷新失败", detail=str(exc))

    if USERNAME and PASSWORD:
        return login_password(session)

    if cookie_fallback:
        log("WARN", "仅配置了 Cookie；本站点使用 Bearer Token，若不成功请改填 REFRESH_TOKEN")
        return None
    raise AuthError("没有可用的登录凭据。请在配置区填 REFRESH_TOKEN（推荐）或 USERNAME/PASSWORD。")


def check_login(session, dry: bool = False) -> tuple[list[dict], int]:
    now = datetime.now()
    obj = request(session, "GET", LIST_PATH,
                  params={"Year": now.year, "Month": now.month},
                  dry=dry, label="GetStuDailyList")
    days = obj.get("data") or []
    prefix = now.strftime("%Y-%m-%d")
    today: list[dict] = []
    for day in days:
        if str(day.get("date", "")).startswith(prefix):
            today = day.get("stuDailyAssList") or []
            break
    return today, len(days)


def punch_one(session, task: dict, pool: list[Path], dry: bool, prepared: dict | None = None) -> dict:
    """上传图片 + 提交打卡。prepared 用于限流重试时复用已上传的图片 id。"""
    state = prepared if prepared is not None else {}
    if "file_ids" not in state:
        file_ids: list[str] = []
        used: list[str] = []
        if task["need_image"]:
            chosen = pick_images(pool, IMAGE_COUNT)
            if not chosen:
                raise PunchError(f"{task['name']} 需要图片，请检查配置区 IMAGE_DIRS")
            for path in chosen:
                filename, content, mime = file_part(path)
                fields = {
                    "bucketName": BUCKET,
                    "filePath": f"student-pc/{datetime.now():%Y%m%d}",
                    "FileType": "1",
                }
                obj = request(session, "POST", UPLOAD_PATH, data=fields,
                              files={"file": (filename, content, mime)},
                              echo=fields, dry=dry, label="UpLoadFormFile")
                file_id = ("dryrun" + uuid.uuid4().hex[:24]) if dry else (obj.get("data") or {}).get("id")
                if not file_id:
                    raise ApiError(f"上传成功但未返回文件 id：{obj}")
                file_ids.append(file_id)
                used.append(path.name)
                log("INFO", "图片上传完成", type=task["name"], file=path.name,
                    size_kb=round(len(content) / 1024, 1), file_id=file_id)
        state["file_ids"] = file_ids
        state["used"] = used

    file_ids, used = state["file_ids"], state["used"]
    payload = {
        "ASSType": task["asstype"],
        "Comment": task["comment"],
        "ImgJson": json.dumps(file_ids, ensure_ascii=False, separators=(",", ":")),
    }
    path = DAILY_PATH if task["endpoint"] == "daily" else ELEVATE_PATH
    if task["endpoint"] == "elevate":
        payload["Tag"] = task["tag"] or "其他"

    log("INFO", "提交打卡", type=task["name"],
        body=json.dumps(payload, ensure_ascii=False))
    obj = request(session, "POST", path, json_body=payload, echo=payload,
                  dry=dry, label=path.rsplit("/", 1)[-1])
    result_id = "(预演)" if dry else obj.get("data")
    log("INFO", "打卡成功", type=task["name"], result_id=result_id)
    return {"name": task["name"], "ok": True, "images": used, "result_id": result_id}


def punch_one_with_retry(session, task, pool, dry):
    """命中速率限制时指数退避重试；重试只重提交，不重复上传图片。"""
    state = {}
    wait = RATE_LIMIT_WAIT
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            return punch_one(session, task, pool, dry, prepared=state)
        except RateLimitError as exc:
            if attempt >= RATE_LIMIT_RETRIES:
                raise
            pause = wait + random.uniform(0, 3)
            log("WARN", "触发速率限制，等待后重试", type=task["name"],
                wait_sec=round(pause, 1), attempt=attempt + 1,
                max_retries=RATE_LIMIT_RETRIES, detail=str(exc))
            time.sleep(pause)
            wait *= 2
    raise PunchError("限流重试逻辑异常")


# ------------------------- 入口 -------------------------
def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="一键自动打卡（默认打完全部 5 种）")
    ap.add_argument("--dry-run", action="store_true", help="只预演，不上传不提交")
    ap.add_argument("--check", action="store_true", help="检查登录态是否有效")
    ap.add_argument("-t", "--types", help="临时只打指定类型，逗号分隔")
    ap.add_argument("-i", "--images", action="append", help="临时覆盖图片路径，可重复")
    ap.add_argument("--cookie", help="临时附加 Cookie（本站点一般不用）")
    ap.add_argument("--refresh-token", help="临时覆盖 refresh_token（不写入文件）")
    args = ap.parse_args()

    cookie = args.cookie or COOKIE or os.environ.get("PUNCH_COOKIE", "")
    if args.refresh_token:
        global REFRESH_TOKEN
        REFRESH_TOKEN = args.refresh_token
    dry = (args.dry_run or DRY_RUN) and not args.check
    _init_log()

    has_auth = any((ACCESS_TOKEN, REFRESH_TOKEN, USERNAME and PASSWORD,
                    cookie, os.environ.get("PUNCH_REFRESH_TOKEN"), load_cache().get("access_token")))
    if not has_auth and not dry:
        print(AUTH_HELP)
        return 2

    session = make_session(cookie=cookie)
    try:
        if not dry:
            token = resolve_token(session, dry=False, cookie_fallback=bool(cookie))
            if token:
                session.headers["Authorization"] = "Bearer " + token
                log("INFO", "认证就绪", scheme="Bearer")

        if args.check:
            today, days = check_login(session, dry=False)
            print(f"登录态有效：当月返回 {days} 天数据，当天已有 {len(today)} 条每日任务记录")
            for r in today:
                print(f"  - assType={r.get('assType')} 文字={r.get('comment')!r} "
                      f"图片={r.get('imgJson')} 积分={r.get('score')}")
            return 0

        tasks = TASKS
        if args.types:
            wanted = [x.strip() for x in args.types.split(",") if x.strip()]
            tasks = [t for t in TASKS if t["name"] in wanted]
            if not tasks:
                print("没有匹配的类型，可选：" + ", ".join(t["name"] for t in TASKS))
                return 2

        need_image = any(t["need_image"] for t in tasks)
        default_pool = collect_images(args.images or IMAGE_DIRS) if need_image else []
        if args.images:
            log("INFO", "图片池就绪", count=len(default_pool))

        existing = None
        if SKIP_IF_DONE and not dry:
            existing, _ = check_login(session, dry=False)
            log("INFO", "查重基线", today_records=len(existing))

        results: list[dict] = []
        for idx, task in enumerate(tasks):
            pool = default_pool
            if task.get("images"):
                spec = task["images"] if isinstance(task["images"], (list, tuple)) else [task["images"]]
                pool = collect_images(spec)

            skipped = (existing is not None and task["endpoint"] == "daily"
                       and any(str(r.get("assType")) == task["asstype"] for r in existing))
            if skipped:
                log("WARN", "当天已有同类记录，跳过", type=task["name"])
                results.append({"name": task["name"], "ok": True,
                                "images": [], "result_id": "已跳过"})
            else:
                try:
                    results.append(punch_one_with_retry(session, task, pool, dry))
                except PunchError as exc:
                    # 单项失败不中断，继续打后面的类型，最后统一汇总
                    log("ERROR", "该项打卡失败，继续下一项", type=task["name"],
                        error_type=type(exc).__name__, detail=str(exc))
                    results.append({"name": task["name"], "ok": False,
                                    "images": [], "result_id": f"失败：{str(exc)[:36]}"})

            if DELAY > 0 and idx < len(tasks) - 1:
                time.sleep(random.uniform(DELAY * 0.7, DELAY * 1.3))

        print("\n" + "=" * 64)
        print("打卡结果" + ("（预演，未真实提交）" if dry else ""))
        print("=" * 64)
        for r in results:
            status = ("失败" if not r["ok"] else
                      "跳过" if r["result_id"] == "已跳过" else "成功")
            imgs = ", ".join(r["images"]) if r["images"] else "-"
            print(f"  [{status}] {r['name']:<8} 图片={imgs[:34]:<34} id={r['result_id']}")
        ok = sum(1 for r in results if r["ok"])
        print(f"\n合计 {len(results)} 项，成功 {ok} 项，失败 {len(results) - ok} 项")
        return 0 if ok == len(results) else 1

    except AuthError as exc:
        log("ERROR", "登录态失效", detail=str(exc))
        print("请更新配置区 REFRESH_TOKEN / 账号密码，或重新抓一次登录包。", file=sys.stderr)
        return 3
    except RateLimitError as exc:
        log("ERROR", "被服务端限流", detail=str(exc))
        print("服务端速率限制：请等 1-2 分钟再试，或调大配置区 DELAY / RATE_LIMIT_WAIT。", file=sys.stderr)
        return 5
    except (ApiError, PunchError) as exc:
        log("ERROR", "执行失败", error_type=type(exc).__name__, detail=str(exc))
        return 4
    except KeyboardInterrupt:
        log("WARN", "用户中断")
        return 130
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
