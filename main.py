"""
Kruzchl API: upload circular video messages and fetch random ones from others.
Uses /data/kruzchl when the HF persistent volume is mounted, else ./data.
"""

from __future__ import annotations

import json
import os
import random
import uuid
from pathlib import Path
from threading import Lock

from fastapi import Cookie, FastAPI, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

SESSION_COOKIE = "kruzhcl_sid"
SESSION_MAX_AGE = 60 * 60 * 24 * 400  # ~400 days
VIEWS_PER_UPLOAD = 5

_store_lock = Lock()


def _set_session_cookie(response: Response, sid: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=sid,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=os.environ.get("KRUZHCL_SECURE_COOKIES", "").lower() in ("1", "true", "yes"),
    )


def _resolve_sid(response: Response, kruzhcl_sid: str | None) -> str:
    if kruzhcl_sid and kruzhcl_sid.strip():
        return kruzhcl_sid.strip()
    sid = uuid.uuid4().hex
    _set_session_cookie(response, sid)
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
    response: Response,
    kruzhcl_sid: str | None = Cookie(default=None, alias=SESSION_COOKIE),
):
    sid = _resolve_sid(response, kruzhcl_sid)
    with _store_lock:
        sessions = _load_json(SESSIONS_FILE)
        st = _session_state(sessions, sid)
        return {
            "uploads": int(st.get("uploads", 0)),
            "views_used": int(st.get("views", 0)),
            "views_remaining": _remaining(st),
            "views_per_upload": VIEWS_PER_UPLOAD,
        }


@app.post("/api/upload")
async def upload_video(
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

    vid = uuid.uuid4().hex
    name = f"{vid}{ext}"
    path = VIDEOS_DIR / name

    body = await file.read()
    if len(body) < 100:
        raise HTTPException(status_code=400, detail="Video too small")

    sid = _resolve_sid(response, kruzhcl_sid)
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
    response: Response,
    kruzhcl_sid: str | None = Cookie(default=None, alias=SESSION_COOKIE),
):
    sid = _resolve_sid(response, kruzhcl_sid)
    with _store_lock:
        sessions = _load_json(SESSIONS_FILE)
        owners = _load_json(OWNERS_FILE)
        st = _session_state(sessions, sid)

        if _remaining(st) <= 0:
            raise HTTPException(
                status_code=403,
                detail="Нет просмотров: сначала запишите свой кружок (1 запись = 5 просмотров чужих).",
            )

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
