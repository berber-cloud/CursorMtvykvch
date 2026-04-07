const MAX_RECORD_MS = 60_000;
const RECORD_LIMIT_SEC = 60;
const SESSION_STORAGE_KEY = "kruzchl_session_id";

const videoEl = document.getElementById("video");
const circleWrap = document.getElementById("circleWrap");
const recordRingProgress = document.getElementById("recordRingProgress");
const recordTimerText = document.getElementById("recordTimerText");
const permOverlay = document.getElementById("permOverlay");
const btnAllowMedia = document.getElementById("btnAllowMedia");
const permError = document.getElementById("permError");
const btnRecord = document.getElementById("btnRecord");
const btnFlip = document.getElementById("btnFlip");
const btnRandom = document.getElementById("btnRandom");
const quotaLine = document.getElementById("quotaLine");
const statusLine = document.getElementById("statusLine");
const statsLine = document.getElementById("statsLine");

let stream = null;
let facingMode = "user";
let recorder = null;
let chunks = [];
let recordTimer = null;
let recordRingRaf = null;
let recordStartedAt = 0;
let playingExternal = false;

function formatCountdown(remainingMs) {
  const totalSec = Math.max(0, Math.ceil(remainingMs / 1000));
  const mm = Math.floor(totalSec / 60);
  const ss = totalSec % 60;
  return `${mm}:${String(ss).padStart(2, "0")}`;
}

function setRecordRingProgress(p) {
  if (!recordRingProgress) return;
  const clamped = Math.min(1, Math.max(0, p));
  recordRingProgress.style.strokeDashoffset = String(100 * (1 - clamped));
}

function startRecordRingAnim() {
  stopRecordRingAnim();
  recordStartedAt = performance.now();
  if (recordTimerText) {
    recordTimerText.hidden = false;
    recordTimerText.textContent = formatCountdown(MAX_RECORD_MS);
  }
  const tick = (now) => {
    const elapsed = now - recordStartedAt;
    const p = elapsed / MAX_RECORD_MS;
    setRecordRingProgress(p);
    if (recordTimerText) {
      recordTimerText.textContent = formatCountdown(MAX_RECORD_MS - elapsed);
    }
    if (p < 1 && recorder && recorder.state === "recording") {
      recordRingRaf = requestAnimationFrame(tick);
    } else {
      setRecordRingProgress(1);
      if (recordTimerText && p >= 1) {
        recordTimerText.textContent = "0:00";
      }
    }
  };
  recordRingRaf = requestAnimationFrame(tick);
}

function stopRecordRingAnim() {
  if (recordRingRaf) {
    cancelAnimationFrame(recordRingRaf);
    recordRingRaf = null;
  }
}

function resetRecordRing() {
  stopRecordRingAnim();
  setRecordRingProgress(0);
  if (circleWrap) circleWrap.classList.remove("is-recording");
  if (recordTimerText) {
    recordTimerText.hidden = true;
    recordTimerText.textContent = "";
  }
}

function showPermError(message) {
  permError.textContent = message;
  permError.hidden = false;
}

function clearPermError() {
  permError.hidden = true;
  permError.textContent = "";
}

function setCameraControlsDisabled(disabled) {
  btnRecord.disabled = disabled;
  btnFlip.disabled = disabled;
}

function rememberSessionId(data) {
  const id = data && data.session_id;
  if (typeof id === "string" && /^[a-f0-9]{32}$/.test(id)) {
    try {
      sessionStorage.setItem(SESSION_STORAGE_KEY, id);
    } catch (_) {
      /* private mode */
    }
  }
}

function sessionFetchHeaders() {
  const sid = (() => {
    try {
      return sessionStorage.getItem(SESSION_STORAGE_KEY);
    } catch (_) {
      return null;
    }
  })();
  return sid ? { "X-Kruzchl-Session": sid } : {};
}

function pickMimeType() {
  const candidates = [
    "video/webm;codecs=vp9,opus",
    "video/webm;codecs=vp8,opus",
    "video/webm",
  ];
  for (const c of candidates) {
    if (typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported(c)) {
      return c;
    }
  }
  return "";
}

async function refreshQuota() {
  try {
    const r = await fetch("/api/quota", {
      credentials: "include",
      headers: { ...sessionFetchHeaders() },
    });
    if (!r.ok) throw new Error("quota");
    const q = await r.json();
    rememberSessionId(q);
    const rem = q.views_remaining ?? 0;
    const up = q.uploads ?? 0;
    quotaLine.textContent =
      up === 0
        ? `Запишите кружок (до ${RECORD_LIMIT_SEC} с) — получите ${q.views_per_upload ?? 5} просмотров чужих`
        : `Ваших записей: ${up}. Осталось просмотров чужих: ${rem}`;
    return q;
  } catch {
    quotaLine.textContent = "Не удалось загрузить квоту";
    return null;
  }
}

async function refreshStats() {
  if (!statsLine) return null;
  try {
    const r = await fetch("/api/stats", { credentials: "include" });
    if (!r.ok) throw new Error("stats");
    const s = await r.json();
    const total = s.total_videos ?? 0;
    statsLine.textContent = `Всего видео: ${total}`;
    return s;
  } catch {
    statsLine.textContent = "";
    return null;
  }
}

async function startCamera() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    throw new Error("Браузер не поддерживает доступ к камере (нужен HTTPS или localhost).");
  }
  if (stream) {
    stream.getTracks().forEach((t) => t.stop());
  }
  stream = await navigator.mediaDevices.getUserMedia({
    audio: true,
    video: {
      facingMode: facingMode,
      width: { ideal: 720 },
      height: { ideal: 720 },
    },
  });
  videoEl.srcObject = stream;
  playingExternal = false;
  videoEl.muted = true;
  await videoEl.play().catch(() => {});
}

function setMirror(forCamera) {
  videoEl.style.setProperty("--mirror", forCamera ? "-1" : "1");
}

btnAllowMedia.addEventListener("click", async () => {
  clearPermError();
  btnAllowMedia.disabled = true;
  try {
    await startCamera();
    setMirror(facingMode === "user");
    permOverlay.hidden = true;
    setCameraControlsDisabled(false);
    statusLine.textContent = "";
    await refreshQuota();
  } catch (e) {
    const name = e && e.name ? e.name : "";
    if (name === "NotAllowedError" || name === "PermissionDeniedError") {
      showPermError("Доступ запрещён. Разрешите камеру и микрофон в настройках сайта в браузере и нажмите снова.");
    } else if (name === "NotFoundError" || name === "DevicesNotFoundError") {
      showPermError("Камера или микрофон не найдены.");
    } else {
      showPermError(e.message || "Не удалось получить доступ к устройствам.");
    }
    console.error(e);
  } finally {
    btnAllowMedia.disabled = false;
  }
});

btnFlip.addEventListener("click", async () => {
  if (playingExternal) return;
  if (!stream) {
    permOverlay.hidden = false;
    showPermError("Сначала разрешите доступ к камере.");
    return;
  }
  facingMode = facingMode === "user" ? "environment" : "user";
  try {
    await startCamera();
    setMirror(facingMode === "user");
  } catch (e) {
    statusLine.textContent = "Не удалось переключить камеру";
    console.error(e);
  }
});

function stopRecording() {
  if (recordTimer) {
    clearTimeout(recordTimer);
    recordTimer = null;
  }
  const rec = recorder;
  if (rec && rec.state === "recording") {
    try {
      if (typeof rec.requestData === "function") {
        rec.requestData();
      }
    } catch (_) {
      /* ignore */
    }
    try {
      rec.stop();
    } catch (_) {
      recorder = null;
      statusLine.textContent = "Не удалось завершить запись. Попробуйте ещё раз.";
    }
  }
  /* recorder очищается в onstop — нельзя занулять до вызова onstop, иначе падает чтение mimeType */
  btnRecord.classList.remove("recording");
  btnRecord.disabled = false;
  resetRecordRing();
  statusLine.textContent = "Сохранение записи…";
}

async function uploadBlob(blob) {
  statusLine.textContent = "Отправка…";
  const fd = new FormData();
  fd.append("file", blob, "kruzh.webm");
  const r = await fetch("/api/upload", {
    method: "POST",
    body: fd,
    credentials: "include",
    headers: { ...sessionFetchHeaders() },
  });
  if (!r.ok) {
    const t = await r.text();
    throw new Error(t || "upload failed");
  }
  const up = await r.json();
  rememberSessionId(up);
  statusLine.textContent = "Готово! Можно смотреть чужие кружки.";
  await refreshQuota();
  await refreshStats();
}

btnRecord.addEventListener("click", async () => {
  if (playingExternal) {
    playingExternal = false;
    videoEl.pause();
    videoEl.removeAttribute("src");
    videoEl.muted = true;
    try {
      await startCamera();
      setMirror(facingMode === "user");
      statusLine.textContent = "";
      await refreshQuota();
    } catch (e) {
      permOverlay.hidden = false;
      showPermError("Снова разрешите доступ к камере и микрофону.");
      console.error(e);
    }
    return;
  }

  if (!stream) {
    permOverlay.hidden = false;
    clearPermError();
    statusLine.textContent = "Нажмите «Разрешить доступ к камере и микрофону»";
    return;
  }

  if (recorder && recorder.state === "recording") {
    stopRecording();
    return;
  }

  chunks = [];
  const mime = pickMimeType();
  try {
    recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
  } catch {
    recorder = new MediaRecorder(stream);
  }

  recorder.ondataavailable = (e) => {
    if (e.data && e.data.size) chunks.push(e.data);
  };

  recorder.onstop = async (ev) => {
    const rec = ev.target;
    recorder = null;
    const type = (rec && rec.mimeType) || "video/webm";
    const blob = new Blob(chunks, { type });
    chunks = [];
    if (blob.size < 200) {
      statusLine.textContent = "Слишком короткая запись";
      return;
    }
    try {
      await uploadBlob(blob);
    } catch (e) {
      statusLine.textContent = "Не удалось отправить видео";
      console.error(e);
    }
  };

  try {
    recorder.start(200);
  } catch (e) {
    btnRecord.classList.remove("recording");
    resetRecordRing();
    statusLine.textContent = "Не удалось начать запись";
    console.error(e);
    return;
  }

  btnRecord.classList.add("recording");
  setRecordRingProgress(0);
  if (circleWrap) circleWrap.classList.add("is-recording");
  startRecordRingAnim();
  statusLine.textContent = `Идёт запись… макс. ${RECORD_LIMIT_SEC} с`;

  recordTimer = setTimeout(() => {
    if (recorder && recorder.state === "recording") {
      stopRecording();
      statusLine.textContent = "Лимит 60 с";
    }
  }, MAX_RECORD_MS);
});

btnRandom.addEventListener("click", async () => {
  statusLine.textContent = "";
  try {
    const r = await fetch("/api/random", {
      credentials: "include",
      headers: { ...sessionFetchHeaders() },
    });
    if (r.status === 403) {
      const err = await r.json().catch(() => ({}));
      statusLine.textContent = err.detail || "Сначала запишите свой кружок";
      await refreshQuota();
      return;
    }
    if (r.status === 404) {
      const err = await r.json().catch(() => ({}));
      statusLine.textContent = err.detail || "Пока нет чужих кружков";
      return;
    }
    if (!r.ok) throw new Error("random");
    const data = await r.json();
    rememberSessionId(data);
    if (stream) {
      stream.getTracks().forEach((t) => t.stop());
      stream = null;
    }
    videoEl.srcObject = null;
    videoEl.src = data.url;
    videoEl.muted = false;
    playingExternal = true;
    setMirror(false);
    await videoEl.play().catch(() => {});
    statusLine.textContent = "Чужой кружок";
    await refreshQuota();
    await refreshStats();
  } catch (e) {
    statusLine.textContent = "Не удалось загрузить случайный кружок";
    console.error(e);
  }
});

videoEl.addEventListener("ended", () => {
  if (playingExternal) {
    statusLine.textContent = "Конец. Запишите свой или откройте ещё один.";
  }
});

(async function init() {
  setMirror(true);
  setCameraControlsDisabled(true);
  clearPermError();

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    permOverlay.hidden = false;
    showPermError("Откройте сайт по HTTPS (или localhost), чтобы браузер разрешил камеру и микрофон.");
    btnAllowMedia.disabled = true;
  }

  await refreshQuota();
  await refreshStats();
})();
