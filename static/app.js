const MAX_RECORD_MS = 60_000;

const videoEl = document.getElementById("video");
const circleFrame = document.getElementById("circleFrame");
const btnRecord = document.getElementById("btnRecord");
const btnFlip = document.getElementById("btnFlip");
const btnRandom = document.getElementById("btnRandom");
const quotaLine = document.getElementById("quotaLine");
const statusLine = document.getElementById("statusLine");

let stream = null;
let facingMode = "user";
let recorder = null;
let chunks = [];
let recordTimer = null;
let playingExternal = false;

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
    const r = await fetch("/api/quota", { credentials: "include" });
    if (!r.ok) throw new Error("quota");
    const q = await r.json();
    const rem = q.views_remaining ?? 0;
    const up = q.uploads ?? 0;
    quotaLine.textContent =
      up === 0
        ? `Запишите кружок — получите ${q.views_per_upload ?? 5} просмотров чужих`
        : `Ваших записей: ${up}. Осталось просмотров чужих: ${rem}`;
    return q;
  } catch {
    quotaLine.textContent = "Не удалось загрузить квоту";
    return null;
  }
}

async function startCamera() {
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

btnFlip.addEventListener("click", async () => {
  if (playingExternal) return;
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
  if (recorder && recorder.state !== "inactive") {
    recorder.stop();
  }
  recorder = null;
  btnRecord.classList.remove("recording");
  btnRecord.disabled = false;
}

async function uploadBlob(blob) {
  statusLine.textContent = "Отправка…";
  const fd = new FormData();
  fd.append("file", blob, "kruzh.webm");
  const r = await fetch("/api/upload", {
    method: "POST",
    body: fd,
    credentials: "include",
  });
  if (!r.ok) {
    const t = await r.text();
    throw new Error(t || "upload failed");
  }
  await r.json();
  statusLine.textContent = "Готово! Можно смотреть чужие кружки.";
  await refreshQuota();
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
      statusLine.textContent = "Не удалось снова включить камеру";
      console.error(e);
    }
    return;
  }
  if (!stream) {
    try {
      await startCamera();
      setMirror(facingMode === "user");
    } catch (e) {
      statusLine.textContent = "Нужен доступ к камере и микрофону";
      return;
    }
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

  recorder.onstop = async () => {
    const type = recorder.mimeType || "video/webm";
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

  recorder.start(200);
  btnRecord.classList.add("recording");
  statusLine.textContent = "Идёт запись…";

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
    const r = await fetch("/api/random", { credentials: "include" });
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
  try {
    await startCamera();
    await refreshQuota();
    statusLine.textContent = "";
  } catch (e) {
    quotaLine.textContent = "Разрешите камеру и микрофон";
    statusLine.textContent = "Нажмите запись после разрешения доступа";
    console.error(e);
  }
})();
