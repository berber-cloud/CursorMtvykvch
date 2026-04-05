"""
Kruzchl API: upload circular video messages and fetch random ones from others.
Uses /data/kruzchl when the HF persistent volume is mounted, else ./data.
Videos can be stored on the Hugging Face Hub dataset (see hub_storage) when HF_KRUZHKI_REPO + HF_TOKEN are set.
"""

from __future__ import annotations

import json
import os
import random
import uuid
from pathlib import Path
from threading import Lock
from urllib.parse import quote, unquote

from fastapi import Cookie, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import hub_storage as hub

SESSION_COOKIE = "kruzhcl_sid"
SESSION_MAX_AGE = 60 * 60 * 24 * 400  # ~400 days
VIEWS_PER_UPLOAD = 5

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


def _resolve_sid(response: Response, request: Request, kruzhcl_sid: str | None) -> str:
    if kruzhcl_sid and kruzhcl_sid.strip():
        return kruzhcl_sid.strip()
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


def _session_state(sessions: dict, sid: str) -> dict:
    if sid not in sessions:
        sessions[sid] = {"uploads": 0, "views": 0}
    return sessions[sid]


def _credits(state: dict) -> int:
    return int(state.get("uploads", 0)) * VIEWS_PER_UPLOAD


def _remaining(state: dict) -> int:
    return max(0, _credits(state) - int(state.get("views", 0)))


app = FastAPI(title="Kruzchl")

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

    if hub.enabled():
        try:
            name = hub.save_video(body, ext, sid)
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"Не удалось сохранить в Hub: {e!s}",
            ) from e
        with _store_lock:
            sessions = _load_json(SESSIONS_FILE)
            st = _session_state(sessions, sid)
            st["uploads"] = int(st.get("uploads", 0)) + 1
            _save_json(SESSIONS_FILE, sessions)
    else:
        vid = uuid.uuid4().hex
        name = f"{vid}{ext}"
        path = VIDEOS_DIR / name
        with _store_lock:
            sessions = _load_json(SESSIONS_FILE)
            owners = _load_json(OWNERS_FILE)
            st = _session_state(sessions, sid)
            st["uploads"] = int(st.get("uploads", 0)) + 1
            path.write_bytes(body)
            owners[name] = sid
            _save_json(SESSIONS_FILE, sessions)
            _save_json(OWNERS_FILE, owners)

    return {
        "ok": True,
        "id": name,
        "views_remaining": _remaining(st),
        "uploads": int(st.get("uploads", 0)),
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
            picked = hub.pick_random_video(sid)
            if not picked:
                raise HTTPException(
                    status_code=404,
                    detail="Пока нет чужих кружков. Загляните позже или попросите друзей записать.",
                )
            rel_path, basename = picked
            st["views"] = int(st.get("views", 0)) + 1
            views_remaining = _remaining(st)
            _save_json(SESSIONS_FILE, sessions)
            enc = quote(rel_path, safe="")
            return {
                "url": f"/media/hub?p={enc}",
                "filename": basename,
                "views_remaining": views_remaining,
            }

        owners = _load_json(OWNERS_FILE)
        candidates = []
        for f in VIDEOS_DIR.iterdir():
            if not f.is_file():
                continue
            if f.suffix.lower() not in (".webm", ".mp4"):
                continue
            owner = owners.get(f.name, None)
            if owner != sid:
                candidates.append(f.name)

        if not candidates:
            raise HTTPException(
                status_code=404,
                detail="Пока нет чужих кружков. Загляните позже или попросите друзей записать.",
            )

        pick = random.choice(candidates)
        st["views"] = int(st.get("views", 0)) + 1
        views_remaining = _remaining(st)
        _save_json(SESSIONS_FILE, sessions)

    return {
        "url": f"/media/{pick}",
        "filename": pick,
        "views_remaining": views_remaining,
    }


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
