"""
Kruzchl API: upload circular video messages and fetch random ones from others.
Uses /data/kruzchl when the HF persistent volume is mounted, else ./data.
Videos can be stored on the Hugging Face Hub dataset (see hub_storage) when HF_KRUZHKI_REPO + HF_TOKEN are set.
"""

from __future__ import annotations

import json
import os
import random
import re
import uuid
import hashlib
import time
from pathlib import Path
from threading import Lock, Thread
from time import sleep
from urllib.parse import quote, unquote

from fastapi import Cookie, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import hub_storage as hub
from moderation import moderate_video_bytes, moderate_video_path

SESSION_COOKIE = "kruzhcl_sid"
SESSION_HEADER = "x-kruzchl-session"
SESSION_MAX_AGE = 60 * 60 * 24 * 400  # ~400 days
VIEWS_PER_UPLOAD = 5
_SID_RE = re.compile(r"^[a-f0-9]{32}$")

_store_lock = Lock()
_metrics_lock = Lock()
_metrics: dict[str, int] = {}


def _metric_inc(name: str, delta: int = 1) -> None:
    with _metrics_lock:
        _metrics[name] = int(_metrics.get(name, 0)) + int(delta)


def _metric_snapshot() -> dict[str, int]:
    with _metrics_lock:
        return dict(_metrics)


def _cookie_secure(request: Request) -> bool:
    """
    HF и другие прокси часто отдают приложению HTTP, а снаружи HTTPS.
    Secure-cookie при этом должна включаться по X-Forwarded-Proto, иначе браузер
    не сохранит сессию и квота «сбрасывается» (нет просмотров после записи).
    """
    force = os.environ.get("KRUZHCL_SECURE_COOKIES", "").lower()
    if force in ("1", "true", "yes"):
        return True
    if force in ("0", "false", "no"):
        return False
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    if forwarded == "https":
        return True
    return request.url.scheme == "https"


def _set_session_cookie(response: Response, request: Request, sid: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=sid,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
    )


def _normalize_sid(value: str | None) -> str | None:
    if not value:
        return None
    s = value.strip()
    return s if _SID_RE.match(s) else None


def _resolve_sid(response: Response, request: Request, kruzhcl_sid: str | None) -> str:
    """
    Сессия: cookie (HttpOnly) или заголовок X-Kruzchl-Session (дублируется в sessionStorage на клиенте).
    Нужно, когда cookie не цепляется (iframe HF, блокировщики, прокси).
    """
    cookie_sid = _normalize_sid(kruzhcl_sid)
    header_sid = _normalize_sid(request.headers.get(SESSION_HEADER))
    sid = cookie_sid or header_sid
    if sid:
        if not cookie_sid and header_sid:
            _set_session_cookie(response, request, sid)
        return sid
    sid = uuid.uuid4().hex
    _set_session_cookie(response, request, sid)
    return sid


def _pick_data_root() -> Path:
    env = os.environ.get("KRUZHCL_DATA")
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.append(Path("/data/kruzchl"))
    candidates.append(Path(__file__).resolve().parent / "data")

    for p in candidates:
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return p
        except OSError:
            continue
    fallback = Path(__file__).resolve().parent / "data"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


DATA_ROOT = _pick_data_root()
VIDEOS_DIR = DATA_ROOT / "videos"
SESSIONS_FILE = DATA_ROOT / "sessions.json"
OWNERS_FILE = DATA_ROOT / "owners.json"
MODERATION_FILE = DATA_ROOT / "moderation_rejected.json"
HUB_SYNC_FILE = DATA_ROOT / "hub_synced.json"
CONTENT_HASHES_FILE = DATA_ROOT / "content_hashes.json"
PHASH_INDEX_FILE = DATA_ROOT / "perceptual_hashes.json"
RATE_LIMITS_FILE = DATA_ROOT / "rate_limits.json"

VIDEOS_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    except OSError:
        return {}


def _save_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(path)


def _now() -> float:
    return float(time.time())


def _client_ip(request: Request) -> str:
    # Best-effort behind proxies (HF, etc.)
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        ip = xff.split(",")[0].strip()
        if ip:
            return ip
    fwd = request.headers.get("forwarded", "")
    if "for=" in fwd:
        try:
            part = fwd.split("for=", 1)[1].split(";", 1)[0].strip().strip('"')
            if part:
                return part
        except Exception:
            pass
    return getattr(getattr(request, "client", None), "host", None) or "unknown"


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name, "").strip()
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _prune_ts(ts: list[float], *, window_s: int, now: float) -> list[float]:
    cutoff = now - float(window_s)
    return [t for t in ts if t >= cutoff]


def _rate_limit_check_and_touch(request: Request, sid: str) -> tuple[bool, str | None]:
    """
    Returns (allowed, reason). Updates counters when allowed.
    """
    sid_per_min = _env_int("KRUZHCL_RATE_SID_PER_MIN", 4)
    ip_per_min = _env_int("KRUZHCL_RATE_IP_PER_MIN", 10)
    window_s = 60
    now = _now()
    ip = _client_ip(request)

    with _store_lock:
        raw = _load_json(RATE_LIMITS_FILE)
        store = raw if isinstance(raw, dict) else {}
        by_sid = store.get("sid", {}) if isinstance(store.get("sid", {}), dict) else {}
        by_ip = store.get("ip", {}) if isinstance(store.get("ip", {}), dict) else {}

        sid_ts = [float(x) for x in (by_sid.get(sid, []) if isinstance(by_sid.get(sid, []), list) else [])]
        ip_ts = [float(x) for x in (by_ip.get(ip, []) if isinstance(by_ip.get(ip, []), list) else [])]
        sid_ts = _prune_ts(sid_ts, window_s=window_s, now=now)
        ip_ts = _prune_ts(ip_ts, window_s=window_s, now=now)

        if sid_per_min > 0 and len(sid_ts) >= sid_per_min:
            _metric_inc("reject.rate_limited_session")
            by_sid[sid] = sid_ts
            store["sid"] = by_sid
            store["ip"] = by_ip
            _save_json(RATE_LIMITS_FILE, store)
            return False, "rate_limited_session"
        if ip_per_min > 0 and len(ip_ts) >= ip_per_min:
            _metric_inc("reject.rate_limited_ip")
            by_ip[ip] = ip_ts
            store["sid"] = by_sid
            store["ip"] = by_ip
            _save_json(RATE_LIMITS_FILE, store)
            return False, "rate_limited_ip"

        sid_ts.append(now)
        ip_ts.append(now)
        # Cap lists to avoid unbounded growth.
        by_sid[sid] = sid_ts[-200:]
        by_ip[ip] = ip_ts[-400:]
        store["sid"] = by_sid
        store["ip"] = by_ip
        _save_json(RATE_LIMITS_FILE, store)
        return True, None


def _sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _exact_dedup_check_and_touch(sha: str) -> tuple[bool, str | None]:
    """
    Returns (allowed, reason). Updates counters.
    """
    max_per_hour = _env_int("KRUZHCL_DUP_SHA_MAX_PER_HOUR", 2)
    window_s = 60 * 60
    now = _now()

    with _store_lock:
        raw = _load_json(CONTENT_HASHES_FILE)
        store = raw if isinstance(raw, dict) else {}
        rec = store.get(sha, {}) if isinstance(store.get(sha, {}), dict) else {}
        ts: list[float] = [float(x) for x in (rec.get("ts", []) if isinstance(rec.get("ts", []), list) else [])]
        ts = _prune_ts(ts, window_s=window_s, now=now)
        if max_per_hour > 0 and len(ts) >= max_per_hour:
            _metric_inc("reject.duplicate_exact")
            rec["ts"] = ts[-50:]
            rec["last"] = now
            rec["count"] = int(rec.get("count", 0)) + 1
            store[sha] = rec
            _save_json(CONTENT_HASHES_FILE, store)
            return False, "duplicate_exact"

        ts.append(now)
        rec["ts"] = ts[-50:]
        rec["first"] = float(rec.get("first", now))
        rec["last"] = now
        rec["count"] = int(rec.get("count", 0)) + 1
        store[sha] = rec
        # Cap total keys (simple eviction).
        if len(store) > _env_int("KRUZHCL_DUP_SHA_MAX_KEYS", 8000):
            # drop random-ish older keys
            keys = list(store.keys())
            random.shuffle(keys)
            for k in keys[: max(1, len(keys) // 10)]:
                store.pop(k, None)
        _save_json(CONTENT_HASHES_FILE, store)
        return True, None


def _hamming64(a_hex: str, b_hex: str) -> int:
    try:
        a = int(a_hex, 16)
        b = int(b_hex, 16)
    except Exception:
        return 64
    return int((a ^ b).bit_count())


def _near_dedup_check_and_touch(fp: str) -> tuple[bool, str | None]:
    """
    fp: 64-bit hex fingerprint string (len 16).
    Returns (allowed, reason). Updates index.
    """
    if not fp or len(fp) < 8:
        return True, None
    max_dist = _env_int("KRUZHCL_DUP_PHASH_MAX_DIST", 6)
    max_hits = _env_int("KRUZHCL_DUP_PHASH_MAX_HITS", 2)
    now = _now()
    window_s = _env_int("KRUZHCL_DUP_PHASH_WINDOW_S", 6 * 60 * 60)

    bucket = fp[:4]
    with _store_lock:
        raw = _load_json(PHASH_INDEX_FILE)
        store = raw if isinstance(raw, dict) else {}
        buckets = store.get("b", {}) if isinstance(store.get("b", {}), dict) else {}
        b = buckets.get(bucket, []) if isinstance(buckets.get(bucket, []), list) else []

        # b entries: {"fp":..., "ts":...}
        kept = []
        hits = 0
        for item in b:
            if not isinstance(item, dict):
                continue
            old_fp = str(item.get("fp", "")).strip()
            old_ts = float(item.get("ts", 0.0) or 0.0)
            if old_ts < (now - float(window_s)):
                continue
            kept.append({"fp": old_fp, "ts": old_ts})
            if old_fp and _hamming64(fp, old_fp) <= max_dist:
                hits += 1

        if max_hits > 0 and hits >= max_hits:
            _metric_inc("reject.duplicate_near")
            kept.append({"fp": fp, "ts": now})
            buckets[bucket] = kept[-200:]
            store["b"] = buckets
            _save_json(PHASH_INDEX_FILE, store)
            return False, "duplicate_near"

        kept.append({"fp": fp, "ts": now})
        buckets[bucket] = kept[-200:]
        store["b"] = buckets
        _save_json(PHASH_INDEX_FILE, store)
        return True, None


def _load_rejected() -> dict[str, str]:
    raw = _load_json(MODERATION_FILE)
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    return {}


def _mark_rejected(video_id: str, reason: str) -> None:
    with _store_lock:
        rej = _load_rejected()
        rej[video_id] = reason
        _save_json(MODERATION_FILE, rej)


def _is_rejected(video_id: str) -> bool:
    rej = _load_rejected()
    return video_id in rej


def _rejection_reason(video_id: str) -> str | None:
    rej = _load_rejected()
    return rej.get(video_id)


def _moderate_or_ok(video_id: str, body: bytes, suffix: str) -> None:
    """
    Raises HTTPException(422) if rejected; otherwise returns None.
    """
    if _is_rejected(video_id):
        reason = _rejection_reason(video_id) or "rejected"
        raise HTTPException(status_code=422, detail=f"Video rejected: {reason}")

    res = moderate_video_bytes(body, suffix=suffix)
    if not res.ok:
        _mark_rejected(video_id, res.reason or "rejected")
        raise HTTPException(status_code=422, detail=f"Video rejected: {res.reason or 'rejected'}")


def _retry(fn, *, attempts: int = 4, base_sleep_s: float = 0.5):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            sleep(base_sleep_s * (2**i))
    if last:
        raise last


def _load_synced() -> dict[str, int]:
    raw = _load_json(HUB_SYNC_FILE)
    if isinstance(raw, dict):
        out: dict[str, int] = {}
        for k, v in raw.items():
            try:
                out[str(k)] = int(v)
            except Exception:
                out[str(k)] = 1
        return out
    return {}


def _mark_synced(name: str) -> None:
    with _store_lock:
        synced = _load_synced()
        synced[name] = 1
        _save_json(HUB_SYNC_FILE, synced)


def _sync_local_to_hub_loop() -> None:
    """
    Best-effort синхронизация локального кэша в Dataset.
    Нужна, чтобы при пиковых сбоях Hub видео не "застревали" навсегда.
    """
    while True:
        try:
            if not hub.enabled():
                sleep(15)
                continue

            owners = _load_json(OWNERS_FILE)
            synced = _load_synced()

            # Берём небольшой батч, чтобы не перегружать Hub.
            batch: list[str] = []
            for f in VIDEOS_DIR.iterdir():
                if not f.is_file() or f.suffix.lower() not in (".webm", ".mp4"):
                    continue
                if f.name in synced:
                    continue
                batch.append(f.name)
                if len(batch) >= 6:
                    break

            if not batch:
                sleep(10)
                continue

            for name in batch:
                if _is_rejected(name):
                    _mark_synced(name)
                    continue
                p = VIDEOS_DIR / name
                if not p.is_file():
                    continue
                sid = str(owners.get(name, "")).strip() or "unknown"
                body = p.read_bytes()
                ext = p.suffix.lower()
                try:
                    _retry(lambda: hub.upload_video_pair(body, ext, sid, name=name), attempts=5)
                    _mark_synced(name)
                except Exception:
                    # Отложим до следующей итерации.
                    pass

        except Exception:
            pass
        sleep(2)


def _premoderate_hub_loop() -> None:
    """
    Фоновая модерация уже существующего датасета: постепенно помечаем спам,
    чтобы он перестал попадать в выдачу.
    """
    while True:
        try:
            if not hub.enabled():
                sleep(20)
                continue

            # Берём небольшой батч (чтобы не перегружать Hub).
            paths = hub.list_video_paths()
            if not paths:
                sleep(20)
                continue
            random.shuffle(paths)

            checked = 0
            for rel in paths[:12]:
                name = Path(rel).name
                if _is_rejected(name):
                    continue
                try:
                    local = hub.local_path_for_playback(rel)
                    b = local.read_bytes()
                    _moderate_or_ok(name, b, suffix=Path(name).suffix.lower() or ".webm")
                except HTTPException:
                    # _moderate_or_ok уже пометил как rejected
                    _metric_inc("premod.hub.rejected")
                    pass
                except Exception:
                    _metric_inc("premod.hub.error")
                    pass
                checked += 1
                if checked >= 4:
                    break
        except Exception:
            pass
        sleep(4)


def _session_state(sessions: dict, sid: str) -> dict:
    if sid not in sessions:
        sessions[sid] = {"uploads": 0, "views": 0}
    return sessions[sid]


def _credits(state: dict) -> int:
    return int(state.get("uploads", 0)) * VIEWS_PER_UPLOAD


def _remaining(state: dict) -> int:
    return max(0, _credits(state) - int(state.get("views", 0)))


app = FastAPI(title="Kruzchl")


@app.on_event("startup")
def _startup():
    t = Thread(target=_sync_local_to_hub_loop, daemon=True)
    t.start()
    t2 = Thread(target=_premoderate_hub_loop, daemon=True)
    t2.start()

app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
    name="static",
)


@app.get("/api/quota")
def get_quota(
    request: Request,
    response: Response,
    kruzhcl_sid: str | None = Cookie(default=None, alias=SESSION_COOKIE),
):
    sid = _resolve_sid(response, request, kruzhcl_sid)
    with _store_lock:
        sessions = _load_json(SESSIONS_FILE)
        st = _session_state(sessions, sid)
        return {
            "uploads": int(st.get("uploads", 0)),
            "views_used": int(st.get("views", 0)),
            "views_remaining": _remaining(st),
            "views_per_upload": VIEWS_PER_UPLOAD,
            "storage": "hub" if hub.enabled() else "local",
            "session_id": sid,
        }


@app.post("/api/upload")
async def upload_video(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    kruzhcl_sid: str | None = Cookie(default=None, alias=SESSION_COOKIE),
):
    if not file.filename and not file.content_type:
        raise HTTPException(status_code=400, detail="Empty upload")

    ext = ".webm"
    ctype = (file.content_type or "").lower()
    if "mp4" in ctype:
        ext = ".mp4"

    sid = _resolve_sid(response, request, kruzhcl_sid)

    allowed, reason = _rate_limit_check_and_touch(request, sid)
    if not allowed:
        raise HTTPException(status_code=429, detail=f"Video rejected: {reason}")

    vid = uuid.uuid4().hex
    name = f"{vid}{ext}"

    # Stream-to-disk: быстрее, меньше RAM, устойчивее под нагрузкой.
    path = VIDEOS_DIR / name
    chunk_size = _env_int("KRUZHCL_UPLOAD_CHUNK", 2 * 1024 * 1024)
    h = hashlib.sha256()
    total = 0
    try:
        with path.open("wb") as out:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                total += len(chunk)
                if total > _env_int("KRUZHCL_MAX_UPLOAD_BYTES", 50 * 1024 * 1024):
                    raise HTTPException(status_code=413, detail="Upload too large")
                h.update(chunk)
                out.write(chunk)
    except HTTPException:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except Exception as e:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=f"Disk write failed: {e!s}") from e

    if total < 100:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(status_code=400, detail="Video too small")

    sha = h.hexdigest()
    allowed, reason = _exact_dedup_check_and_touch(sha)
    if not allowed:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return JSONResponse(status_code=422, content={"error": f"Video rejected: {reason}", "reason": reason})

    # Модерация на входе (при записи).
    try:
        if _is_rejected(name):
            raise HTTPException(status_code=422, detail=f"Video rejected: {_rejection_reason(name) or 'rejected'}")
        res = moderate_video_path(str(path))
        if not res.ok:
            _mark_rejected(name, res.reason or "rejected")
            _metric_inc(f"reject.moderation.{res.reason or 'rejected'}")
            raise HTTPException(status_code=422, detail=f"Video rejected: {res.reason or 'rejected'}")
        if getattr(res, "fingerprint", None):
            allowed, reason = _near_dedup_check_and_touch(str(res.fingerprint))
            if not allowed:
                _mark_rejected(name, reason or "duplicate_near")
                _metric_inc(f"reject.moderation.{reason or 'duplicate_near'}")
                raise HTTPException(status_code=422, detail=f"Video rejected: {reason or 'duplicate_near'}")
    except HTTPException as e:
        # Явный JSON-ответ для клиента: причина в result.reason.
        if e.status_code == 422:
            msg = str(e.detail or "Video rejected")
            reason = msg.split(":", 1)[1].strip() if ":" in msg else "rejected"
            return JSONResponse(
                status_code=422,
                content={"error": f"Video rejected: {reason}", "reason": reason},
            )
        raise

    _metric_inc("upload.accepted")

    stored_as = "local"
    if hub.enabled():
        # Local-first: Hub отправляется фоновым синком, чтобы upload отвечал быстро.
        stored_as = "local_queued"

    with _store_lock:
        sessions = _load_json(SESSIONS_FILE)
        owners = _load_json(OWNERS_FILE)
        st = _session_state(sessions, sid)
        st["uploads"] = int(st.get("uploads", 0)) + 1

        owners[name] = sid
        _save_json(SESSIONS_FILE, sessions)
        _save_json(OWNERS_FILE, owners)

    return {
        "ok": True,
        "id": name,
        "views_remaining": _remaining(st),
        "uploads": int(st.get("uploads", 0)),
        "session_id": sid,
        "stored_as": stored_as,
    }


@app.get("/api/random")
def random_video(
    request: Request,
    response: Response,
    kruzhcl_sid: str | None = Cookie(default=None, alias=SESSION_COOKIE),
):
    sid = _resolve_sid(response, request, kruzhcl_sid)
    with _store_lock:
        sessions = _load_json(SESSIONS_FILE)
        st = _session_state(sessions, sid)

        if _remaining(st) <= 0:
            raise HTTPException(
                status_code=403,
                detail="Нет просмотров: сначала запишите свой кружок (1 запись = 5 просмотров чужих).",
            )

        if hub.enabled():
            # Lazy‑модерация для уже существующего датасета: пробуем несколько кандидатов.
            for _ in range(12):
                picked = hub.pick_random_video(sid)
                if not picked:
                    break
                rel_path, basename = picked
                if _is_rejected(basename):
                    continue
                try:
                    local = hub.local_path_for_playback(rel_path)
                    b = local.read_bytes()
                    _moderate_or_ok(basename, b, suffix=Path(basename).suffix.lower() or ".webm")
                except HTTPException:
                    continue
                except Exception:
                    # Если модерация/скачивание сломались — не блокируем выдачу полностью.
                    pass

                st["views"] = int(st.get("views", 0)) + 1
                views_remaining = _remaining(st)
                _save_json(SESSIONS_FILE, sessions)
                enc = quote(rel_path, safe="")
                return {
                    "url": f"/media/hub?p={enc}",
                    "filename": basename,
                    "views_remaining": views_remaining,
                    "session_id": sid,
                }

            raise HTTPException(
                status_code=404,
                detail="Пока нет чужих кружков. Загляните позже или попросите друзей записать.",
            )

        owners = _load_json(OWNERS_FILE)
        candidates: list[str] = []
        for f in VIDEOS_DIR.iterdir():
            if not f.is_file():
                continue
            if f.suffix.lower() not in (".webm", ".mp4"):
                continue
            if _is_rejected(f.name):
                continue
            owner = owners.get(f.name, None)
            if owner != sid:
                candidates.append(f.name)

        if not candidates:
            raise HTTPException(
                status_code=404,
                detail="Пока нет чужих кружков. Загляните позже или попросите друзей записать.",
            )

        # Lazy‑модерация старых локальных файлов.
        pick = None
        random.shuffle(candidates)
        for cand in candidates[:20]:
            try:
                b = (VIDEOS_DIR / cand).read_bytes()
                _moderate_or_ok(cand, b, suffix=Path(cand).suffix.lower())
                pick = cand
                break
            except HTTPException:
                continue
            except Exception:
                pick = cand
                break

        if not pick:
            raise HTTPException(
                status_code=404,
                detail="Пока нет чужих кружков. Загляните позже или попросите друзей записать.",
            )
        st["views"] = int(st.get("views", 0)) + 1
        views_remaining = _remaining(st)
        _save_json(SESSIONS_FILE, sessions)

    return {
        "url": f"/media/{pick}",
        "filename": pick,
        "views_remaining": views_remaining,
        "session_id": sid,
    }


@app.get("/api/stats")
def stats():
    if hub.enabled():
        total_files = hub.count_kruzhki_files()
        total_videos = total_files // 2
        return {"storage": "hub", "total_videos": total_videos}
    total_videos = 0
    try:
        total_videos = sum(
            1
            for f in VIDEOS_DIR.iterdir()
            if f.is_file() and f.suffix.lower() in (".webm", ".mp4")
        )
    except OSError:
        total_videos = 0
    return {"storage": "local", "total_videos": total_videos}


@app.get("/api/health")
def health():
    # Лёгкий endpoint для наблюдаемости (без внешних зависимостей).
    return {"ok": True, "metrics": _metric_snapshot()}


@app.get("/api/ads-config")
def ads_config():
    """
    A-ADS banner config via env.
    - AADS_UNIT_ID: numeric id for data-aa attribute
    - AADS_SRC: optional iframe src (default //acceptable.a-ads.com/1)
    """
    unit = os.environ.get("AADS_UNIT_ID", "").strip()
    if not unit or not unit.isdigit():
        return {"enabled": False}
    src = os.environ.get("AADS_SRC", "").strip() or "//acceptable.a-ads.com/1"
    return {"enabled": True, "unit_id": unit, "src": src}


@app.get("/media/hub")
def media_hub(p: str = Query(..., min_length=3, max_length=512)):
    if not hub.enabled():
        raise HTTPException(status_code=404)
    raw = unquote(p)
    if ".." in raw or raw.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not raw.startswith(f"{hub.PREFIX}/"):
        raise HTTPException(status_code=400, detail="Invalid path")
    try:
        local = hub.local_path_for_playback(raw)
    except Exception:
        raise HTTPException(status_code=404)
    suffix = Path(raw).suffix.lower()
    media = "video/webm" if suffix == ".webm" else "video/mp4"
    return FileResponse(local, media_type=media, content_disposition_type="inline")


@app.get("/media/{filename}")
def media_file(filename: str):
    safe = Path(filename).name
    path = VIDEOS_DIR / safe
    if not path.is_file():
        raise HTTPException(status_code=404)
    suffix = path.suffix.lower()
    media = "video/webm" if suffix == ".webm" else "video/mp4"
    return FileResponse(path, media_type=media, content_disposition_type="inline")


@app.get("/")
def index():
    return FileResponse(Path(__file__).resolve().parent / "static" / "index.html")
