from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class ModerationResult:
    ok: bool
    reason: str | None = None


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name, "").strip()
    if not v:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def moderate_video_bytes(body: bytes, suffix: str) -> ModerationResult:
    """
    Отсеивает только очевидные случаи:
    - почти полностью чёрный экран
    - камера почти всё время направлена на однотонную поверхность (потолок/стена)
    """
    sample_frames = int(_env_float("KRUZHCL_MOD_FRAMES", 18))
    max_decode_frames = int(_env_float("KRUZHCL_MOD_MAX_DECODE", 240))
    black_mean_thr = _env_float("KRUZHCL_MOD_BLACK_MEAN", 10.0)
    black_std_thr = _env_float("KRUZHCL_MOD_BLACK_STD", 10.0)
    flat_lap_thr = _env_float("KRUZHCL_MOD_FLAT_LAPLACIAN", 18.0)
    flat_motion_thr = _env_float("KRUZHCL_MOD_FLAT_MOTION", 1.2)
    flat_sat_thr = _env_float("KRUZHCL_MOD_FLAT_SAT", 28.0)
    short_sec = _env_float("KRUZHCL_MOD_SHORT_SECONDS", 5.0)
    min_sec = _env_float("KRUZHCL_MOD_MIN_SECONDS", 1.0)
    short_flat_lap_thr = _env_float("KRUZHCL_MOD_SHORT_FLAT_LAPLACIAN", 14.0)
    short_flat_motion_thr = _env_float("KRUZHCL_MOD_SHORT_FLAT_MOTION", 2.0)
    short_flat_sat_thr = _env_float("KRUZHCL_MOD_SHORT_FLAT_SAT", 34.0)

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
            f.write(body)
            tmp_path = f.name

        cap = cv2.VideoCapture(tmp_path)
        if not cap.isOpened():
            return ModerationResult(ok=True)

        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
        duration_s = (frame_count / fps) if fps > 0.1 and frame_count > 0.1 else None

        if duration_s is not None and duration_s < min_sec:
            cap.release()
            return ModerationResult(ok=False, reason="too_short")

        means: list[float] = []
        stds: list[float] = []
        laps: list[float] = []
        motions: list[float] = []
        sats: list[float] = []

        prev_gray: np.ndarray | None = None
        decoded = 0
        kept = 0

        # Для коротких видео читаем плотнее.
        effective_max_decode = max_decode_frames
        effective_samples = sample_frames
        if duration_s is not None and duration_s < short_sec:
            effective_max_decode = max_decode_frames
            effective_samples = max(sample_frames, 28)

        while decoded < effective_max_decode and kept < effective_samples:
            ok, frame = cap.read()
            decoded += 1
            if not ok or frame is None:
                break

            # Берём каждый N-й кадр, чтобы быть дешевле.
            stride = max(1, effective_max_decode // max(1, effective_samples))
            if (decoded % stride) != 0:
                continue

            h, w = frame.shape[:2]
            if h <= 0 or w <= 0:
                continue

            # Нормализация размера ускоряет и сглаживает шум.
            scale_w = 224
            if w != scale_w:
                scale_h = max(1, int(h * (scale_w / w)))
                frame = cv2.resize(frame, (scale_w, scale_h), interpolation=cv2.INTER_AREA)

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            means.append(float(np.mean(gray)))
            stds.append(float(np.std(gray)))

            lap = cv2.Laplacian(gray, cv2.CV_64F)
            laps.append(float(lap.var()))

            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            sats.append(float(np.mean(hsv[:, :, 1])))

            if prev_gray is not None:
                diff = cv2.absdiff(gray, prev_gray)
                motions.append(float(np.mean(diff)))
            prev_gray = gray
            kept += 1

        cap.release()

        if not means:
            return ModerationResult(ok=True)

        # 1) Чёрный экран: почти все кадры тёмные и низкодисперсные.
        blackish = [
            (m < black_mean_thr and s < black_std_thr) for (m, s) in zip(means, stds)
        ]
        if sum(blackish) >= max(3, int(0.8 * len(blackish))):
            return ModerationResult(ok=False, reason="black_screen")

        # 2) Стена/потолок: почти нет движения + очень мало текстуры + низкая насыщенность.
        if motions:
            motion_med = float(np.median(motions))
        else:
            motion_med = 0.0
        lap_med = float(np.median(laps)) if laps else 0.0
        sat_med = float(np.median(sats)) if sats else 0.0

        if duration_s is not None and duration_s < short_sec:
            # Для коротких видео ужесточаем: спам часто почти статичен и однотонен.
            if (
                motion_med < short_flat_motion_thr
                and lap_med < short_flat_lap_thr
                and sat_med < short_flat_sat_thr
            ):
                return ModerationResult(ok=False, reason="flat_surface_short")
        else:
            if motion_med < flat_motion_thr and lap_med < flat_lap_thr and sat_med < flat_sat_thr:
                return ModerationResult(ok=False, reason="flat_surface")

        return ModerationResult(ok=True)
    except Exception:
        # Модерация не должна ломать сервис.
        return ModerationResult(ok=True)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
