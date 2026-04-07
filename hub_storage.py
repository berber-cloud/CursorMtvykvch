"""
Optional storage of video blobs in a Hugging Face Dataset repo (Hub «bucket»).
Set HF_KRUZHKI_REPO=user/dataset-name and HF_TOKEN (Space secret / env).
Each video: kruzhki/<id>.webm plus kruzhki/<id>.webm.owner (session id, text).
"""

from __future__ import annotations

import os
import random
import uuid
import io
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download

PREFIX = "kruzhki"


def _hub_revision() -> str | None:
    r = os.environ.get("HF_KRUZHKI_REVISION", "").strip()
    return r or None


def _repo() -> str:
    return os.environ.get("HF_KRUZHKI_REPO", "").strip()


def _token() -> str | None:
    t = (
        os.environ.get("HF_TOKEN", "").strip()
        or os.environ.get("HUGGING_FACE_HUB_TOKEN", "").strip()
    )
    return t or None


def enabled() -> bool:
    return bool(_repo() and _token())


def _api() -> HfApi:
    return HfApi(token=_token())


def upload_video_pair(body: bytes, ext: str, owner_sid: str, *, name: str | None = None) -> str:
    """
    Upload video + owner sidecar in a single commit. Returns basename e.g. uuid.webm.
    """
    repo = _repo()
    api = _api()
    vid = (name or uuid.uuid4().hex).split(".", 1)[0]
    filename = name or f"{vid}{ext}"
    rel_video = f"{PREFIX}/{filename}"
    rel_owner = f"{PREFIX}/{filename}.owner"

    kw: dict = {"repo_id": repo, "repo_type": "dataset"}
    rev = _hub_revision()
    if rev:
        kw["revision"] = rev

    ops = [
        CommitOperationAdd(path_in_repo=rel_video, path_or_fileobj=io.BytesIO(body)),
        CommitOperationAdd(
            path_in_repo=rel_owner,
            path_or_fileobj=io.BytesIO(owner_sid.encode("utf-8")),
        ),
    ]
    api.create_commit(
        operations=ops,
        commit_message=f"kruzchl video {filename}",
        **kw,
    )
    return filename


def save_video(body: bytes, ext: str, owner_sid: str) -> str:
    """Upload video + owner sidecar. Returns basename e.g. uuid.webm."""
    return upload_video_pair(body, ext, owner_sid)


def _normalize_hub_path(item) -> str:
    p = str(getattr(item, "path", item)).replace("\\", "/").strip()
    if p.startswith("/"):
        p = p[1:]
    return p


def _list_kruzhki_video_paths(api: HfApi, repo: str) -> list[str]:
    """
    Полный список файлов в dataset, затем фильтр по префиксу kruzhki/.
    list_repo_files(..., path_in_repo=...) на Hub часто даёт пустой список — из‑за этого
    «нет чужих кружков» при заполненном репозитории.
    """
    rev = _hub_revision()
    kw: dict = {"repo_id": repo, "repo_type": "dataset"}
    if rev:
        kw["revision"] = rev
    try:
        raw = api.list_repo_files(**kw)
    except Exception:
        return []

    prefix = f"{PREFIX}/"
    videos: list[str] = []
    for item in raw:
        p = _normalize_hub_path(item)
        low = p.lower()
        if not low.startswith(prefix.lower()):
            continue
        if low.endswith(".owner"):
            continue
        if low.endswith(".webm") or low.endswith(".mp4"):
            videos.append(p)
    return videos


def count_kruzhki_files() -> int:
    """
    Всего файлов под kruzhki/ (видео + .owner). В датасете 2 файла на 1 видео,
    поэтому количество видео = files // 2.
    """
    if not enabled():
        return 0
    api = _api()
    repo = _repo()
    rev = _hub_revision()
    kw: dict = {"repo_id": repo, "repo_type": "dataset"}
    if rev:
        kw["revision"] = rev
    try:
        raw = api.list_repo_files(**kw)
    except Exception:
        return 0
    prefix = f"{PREFIX}/".lower()
    n = 0
    for item in raw:
        p = _normalize_hub_path(item).lower()
        if p.startswith(prefix):
            n += 1
    return n


def _owner_for_video(api: HfApi, repo: str, video_rel_path: str) -> str | None:
    owner_rel = f"{video_rel_path}.owner"
    try:
        kw = {
            "repo_id": repo,
            "filename": owner_rel,
            "repo_type": "dataset",
            "token": _token(),
        }
        r = _hub_revision()
        if r:
            kw["revision"] = r
        p = hf_hub_download(**kw)
        return Path(p).read_text(encoding="utf-8").strip()
    except Exception:
        return None


def _sid_match(a: str | None, b: str | None) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


def pick_random_video(exclude_owner_sid: str) -> tuple[str, str] | None:
    """
    Returns (relative_path_in_repo, basename) for a random video not owned by exclude_owner_sid.
    """
    repo = _repo()
    api = _api()
    videos = _list_kruzhki_video_paths(api, repo)
    if not videos:
        return None

    random.shuffle(videos)

    for rel in videos:
        owner = _owner_for_video(api, repo, rel)
        if owner is None or not _sid_match(owner, exclude_owner_sid):
            return rel, Path(rel).name

    return None


def list_video_paths() -> list[str]:
    """List relative kruzhki/<id>.(webm|mp4) paths in the dataset."""
    if not enabled():
        return []
    repo = _repo()
    api = _api()
    return _list_kruzhki_video_paths(api, repo)


def local_path_for_playback(rel_path: str) -> Path:
    """Download (cached) file from Hub; returns local path for FileResponse."""
    safe = rel_path.lstrip("/").replace("..", "")
    kw = {
        "repo_id": _repo(),
        "filename": safe,
        "repo_type": "dataset",
        "token": _token(),
    }
    r = _hub_revision()
    if r:
        kw["revision"] = r
    return Path(hf_hub_download(**kw))
