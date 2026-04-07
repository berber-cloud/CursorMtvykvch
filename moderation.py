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


def _image_entropy(gray: np.ndarray) -> float:
    """Вычисляет энтропию изображения (меру текстурной сложности)."""
    hist = cv2.calcHist([gray], [0], None, [256], [0, 256])
    hist = hist.flatten() / hist.sum()
    hist = hist[hist > 0]
    return -np.sum(hist * np.log2(hist))


def _edge_ratio(gray: np.ndarray) -> float:
    """Доля пикселей, являющихся границами (детектор Canny)."""
    edges = cv2.Canny(gray, 50, 150)
    return np.sum(edges > 0) / gray.size


def moderate_video_bytes(body: bytes, suffix: str = ".webm") -> ModerationResult:
    """
    Модерация видео:
    - Отбраковка почти полностью чёрного экрана.
    - Отбраковка статичного видео, направленного на стену/потолок,
      даже если есть небольшое движение пальца или шум камеры.
    """
    # Настройки (можно менять через переменные окружения)
    sample_frames = int(_env_float("KRUZHCL_MOD_FRAMES", 24))
    max_decode_frames = int(_env_float("KRUZHCL_MOD_MAX_DECODE", 300))

    # Пороги для чёрного экрана
    black_mean_thr = _env_float("KRUZHCL_MOD_BLACK_MEAN", 15.0)      # средняя яркость <15
    black_std_thr = _env_float("KRUZHCL_MOD_BLACK_STD", 12.0)        # низкая контрастность

    # Пороги для стены/потолка (ужесточены против "пальца на камере")
    flat_lap_thr = _env_float("KRUZHCL_MOD_FLAT_LAPLACIAN", 25.0)     # низкая текстурность
    flat_motion_thr = _env_float("KRUZHCL_MOD_FLAT_MOTION", 2.5)      # почти нет движения
    flat_sat_thr = _env_float("KRUZHCL_MOD_FLAT_SAT", 35.0)           # низкая насыщенность
    flat_entropy_thr = _env_float("KRUZHCL_MOD_FLAT_ENTROPY", 3.5)    # энтропия текстуры
    flat_edges_thr = _env_float("KRUZHCL_MOD_FLAT_EDGES", 0.012)      # доля границ <1.2%

    short_sec = _env_float("KRUZHCL_MOD_SHORT_SECONDS", 5.0)
    min_sec = _env_float("KRUZHCL_MOD_MIN_SECONDS", 1.0)

    # Для коротких видео – более мягкие пороги (чтобы случайно не отсечь нормальное)
    short_flat_lap_thr = _env_float("KRUZHCL_MOD_SHORT_FLAT_LAPLACIAN", 20.0)
    short_flat_motion_thr = _env_float("KRUZHCL_MOD_SHORT_FLAT_MOTION", 3.5)
    short_flat_sat_thr = _env_float("KRUZHCL_MOD_SHORT_FLAT_SAT", 45.0)
    short_flat_entropy_thr = _env_float("KRUZHCL_MOD_SHORT_FLAT_ENTROPY", 2.8)
    short_flat_edges_thr = _env_float("KRUZHCL_MOD_SHORT_FLAT_EDGES", 0.008)

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
            f.write(body)
            tmp_path = f.name

        cap = cv2.VideoCapture(tmp_path)
        if not cap.isOpened():
            return ModerationResult(ok=True, reason=None)

        fps = cap.get(cv2.CAP_PROP_FPS)
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        duration_s = (frame_count / fps) if fps > 0.1 and frame_count > 0.1 else None

        if duration_s is not None and duration_s < min_sec:
            cap.release()
            return ModerationResult(ok=False, reason="too_short")

        # Собираем метрики по кадрам
        means = []      # средняя яркость
        stds = []       # стандартное отклонение яркости
        laps = []       # дисперсия лапласиана (текстурность)
        motions = []    # среднее абсолютное различие между соседними кадрами
        sats = []       # средняя насыщенность
        entropies = []  # энтропия
        edges_ratio = [] # доля границ

        prev_gray = None
        decoded = 0
        kept = 0

        # Для коротких видео увеличиваем количество выборок
        effective_max_decode = max_decode_frames
        effective_samples = sample_frames
        if duration_s is not None and duration_s < short_sec:
            effective_samples = max(sample_frames, 32)

        while decoded < effective_max_decode and kept < effective_samples:
            ret, frame = cap.read()
            decoded += 1
            if not ret or frame is None:
                break

            # Стратегия пропуска кадров для равномерной выборки
            stride = max(1, effective_max_decode // max(1, effective_samples))
            if (decoded % stride) != 0:
                continue

            h, w = frame.shape[:2]
            if h <= 0 or w <= 0:
                continue

            # Нормализуем размер для ускорения
            target_w = 224
            if w != target_w:
                target_h = int(h * (target_w / w))
                frame = cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_AREA)

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            mean_val = float(np.mean(gray))
            std_val = float(np.std(gray))
            means.append(mean_val)
            stds.append(std_val)

            # Текстурность через лапласиан
            lap = cv2.Laplacian(gray, cv2.CV_64F)
            laps.append(float(lap.var()))

            # Насыщенность
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            sats.append(float(np.mean(hsv[:, :, 1])))

            # Энтропия и доля границ
            entropies.append(_image_entropy(gray))
            edges_ratio.append(_edge_ratio(gray))

            # Движение между кадрами
            if prev_gray is not None:
                diff = cv2.absdiff(gray, prev_gray)
                motions.append(float(np.mean(diff)))
            prev_gray = gray
            kept += 1

        cap.release()

        if not means:
            return ModerationResult(ok=True, reason=None)

        # 1. Детекция чёрного экрана (тёмный + малая вариативность)
        blackish = [(m < black_mean_thr and s < black_std_thr) for m, s in zip(means, stds)]
        if sum(blackish) >= max(3, int(0.75 * len(blackish))):
            return ModerationResult(ok=False, reason="black_screen")

        # 2. Детекция стены/потолка (статичное, плоское, низкая насыщенность)
        motion_med = float(np.median(motions)) if motions else 0.0
        lap_med = float(np.median(laps)) if laps else 0.0
        sat_med = float(np.median(sats)) if sats else 0.0
        entropy_med = float(np.median(entropies)) if entropies else 0.0
        edges_med = float(np.median(edges_ratio)) if edges_ratio else 0.0

        is_short = duration_s is not None and duration_s < short_sec

        if is_short:
            # Для коротких видео – чуть либеральнее, но всё равно отсекаем совсем пустые
            if (motion_med < short_flat_motion_thr and
                lap_med < short_flat_lap_thr and
                sat_med < short_flat_sat_thr and
                entropy_med < short_flat_entropy_thr and
                edges_med < short_flat_edges_thr):
                return ModerationResult(ok=False, reason="flat_surface_short")
        else:
            # Обычные видео
            if (motion_med < flat_motion_thr and
                lap_med < flat_lap_thr and
                sat_med < flat_sat_thr and
                entropy_med < flat_entropy_thr and
                edges_med < flat_edges_thr):
                return ModerationResult(ok=False, reason="flat_surface")

        return ModerationResult(ok=True, reason=None)

    except Exception:
        # Модерация не должна ломать сервис – пропускаем видео при любой ошибке
        return ModerationResult(ok=True, reason=None)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
