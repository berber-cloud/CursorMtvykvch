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

from huggingface_hub import HfApi, hf_hub_download

PREFIX = "kruzhki"


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


def save_video(body: bytes, ext: str, owner_sid: str) -> str:
    """Upload video + owner sidecar. Returns basename e.g. uuid.webm."""
    repo = _repo()
    api = _api()
    vid = uuid.uuid4().hex
    name = f"{vid}{ext}"
    rel_video = f"{PREFIX}/{name}"
    rel_owner = f"{PREFIX}/{name}.owner"

    api.upload_file(
        path_or_fileobj=io.BytesIO(body),
        path_in_repo=rel_video,
        repo_id=repo,
        repo_type="dataset",
        commit_message=f"kruzchl video {name}",
    )
    api.upload_file(
        path_or_fileobj=io.BytesIO(owner_sid.encode("utf-8")),
        path_in_repo=rel_owner,
        repo_id=repo,
        repo_type="dataset",
        commit_message=f"kruzchl owner {name}",
    )
    return name


def _owner_for_video(api: HfApi, repo: str, video_rel_path: str) -> str | None:
    owner_rel = f"{video_rel_path}.owner"
    try:
        p = hf_hub_download(
            repo_id=repo,
            filename=owner_rel,
            repo_type="dataset",
            token=_token(),
        )
        return Path(p).read_text(encoding="utf-8").strip()
    except Exception:
        return None


def pick_random_video(exclude_owner_sid: str) -> tuple[str, str] | None:
    """
    Returns (relative_path_in_repo, basename) for a random video not owned by exclude_owner_sid.
    """
    repo = _repo()
    api = _api()
    try:
        raw = api.list_repo_files(
            repo_id=repo,
            repo_type="dataset",
            path_in_repo=PREFIX,
        )
    except Exception:
        return None

    paths = []
    for item in raw:
        p = str(getattr(item, "path", item)).replace("\\", "/")
        if not p.startswith(f"{PREFIX}/"):
            p = f"{PREFIX}/{p.lstrip('/')}"
        paths.append(p)

    videos = [
        p
        for p in paths
        if (p.endswith(".webm") or p.endswith(".mp4")) and not p.endswith(".owner")
    ]
    random.shuffle(videos)

    for rel in videos:
        owner = _owner_for_video(api, repo, rel)
        if owner is None or owner != exclude_owner_sid:
            return rel, Path(rel).name

    return None


def local_path_for_playback(rel_path: str) -> Path:
    """Download (cached) file from Hub; returns local path for FileResponse."""
    safe = rel_path.lstrip("/").replace("..", "")
    return Path(
        hf_hub_download(
            repo_id=_repo(),
            filename=safe,
            repo_type="dataset",
            token=_token(),
        )
    )
