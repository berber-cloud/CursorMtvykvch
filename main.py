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
from pathlib import Path
from threading import Lock, Thread
from time import sleep
from urllib.parse import quote, unquote

from fastapi import Cookie, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import hub_storage as hub
from moderation import moderate_video_bytes

SESSION_COOKIE = "kruzhcl_sid"
SESSION_HEADER = "x-kruzchl-session"
SESSION_MAX_AGE = 60 * 60 * 24 * 400  # ~400 days
VIEWS_PER_UPLOAD = 5
_SID_RE = re.compile(r"^[a-f0-9]{32}$")

_store_lock = Lock()


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


def _moderate_or_ok(video_id: str, body: bytes, ext: str) -> None:
    """
    Raises HTTPException(422) if rejected; otherwise returns None.
    """
    if _is_rejected(video_id):
        raise HTTPException(status_code=422, detail="Видео отклонено модерацией.")
    res = moderate_video_bytes(body, ext)
    if not res.ok:
        _mark_rejected(video_id, res.reason or "rejected")
        raise HTTPException(
            status_code=422,
            detail="Видео отклонено модерацией (чёрный экран/потолок/стена).",
        )


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

    body = await file.read()
    if len(body) < 100:
        raise HTTPException(status_code=400, detail="Video too small")

    sid = _resolve_sid(response, request, kruzhcl_sid)

    vid = uuid.uuid4().hex
    name = f"{vid}{ext}"

    # Модерация на входе (при записи).
    _moderate_or_ok(name, body, ext)

    stored_as = "local"
    if hub.enabled():
        # Не даём сервису падать при пиках/сетевых сбоях: ретраи, затем фолбэк на локальный диск.
        try:
            stored_as = "hub"
            _retry(lambda: hub.upload_video_pair(body, ext, sid, name=name))
            _mark_synced(name)
        except Exception:
            stored_as = "local_fallback"

    path = VIDEOS_DIR / name
    with _store_lock:
        sessions = _load_json(SESSIONS_FILE)
        owners = _load_json(OWNERS_FILE)
        st = _session_state(sessions, sid)
        st["uploads"] = int(st.get("uploads", 0)) + 1

        # Всегда сохраняем локально (и как кэш, и как защита от сбоев Hub).
        try:
            path.write_bytes(body)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Disk write failed: {e!s}") from e

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
                    _moderate_or_ok(basename, b, Path(basename).suffix.lower() or ".webm")
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
                _moderate_or_ok(cand, b, Path(cand).suffix.lower())
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
