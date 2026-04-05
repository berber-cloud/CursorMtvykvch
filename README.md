---
title: Kruzchl
emoji: 📹
colorFrom: gray
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

# Kruzchl

Space: [huggingface.co/spaces/Matveykovich/Kruzchl](https://huggingface.co/spaces/Matveykovich/Kruzchl)

Круглые видеосообщения («кружки»): запись в браузере и просмотр случайных кружков других пользователей. **1 запись = 5 просмотров** чужих видео.

Файлы по умолчанию — на диске Space (для сохранения между перезапусками: [persistent storage](https://huggingface.co/docs/hub/spaces-storage), каталог `/data`).

### Хранение кружков в Hugging Face (Dataset)

1. Создайте **пустой Dataset** на Hub, например `ВашНик/kruzchl-videos` (можно приватный).
2. В настройках **Space** → **Variables and secrets** добавьте:
   - **Secret** `HF_TOKEN` — токен с правом **записи** в этот dataset (и чтения списка файлов).
   - **Variable** `HF_KRUZHKI_REPO` = `ВашНик/kruzchl-videos` (точное имя репозитория).
3. Перезапустите Space. Видео уходят в ветку `main` репозитория в папку `kruzhki/`; рядом лежат маленькие файлы `.owner` с id сессии (для квоты «не показывать свои»).

Если `HF_KRUZHKI_REPO` не задан, используется локальная папка `videos/` как раньше.

## Локальный запуск

```bash
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 7860
```

Откройте http://127.0.0.1:7860

## GitHub → Hugging Face

В настройках репозитория GitHub добавьте секрет `HF_TOKEN` — [Personal Access Token](https://huggingface.co/settings/tokens) или **fine-grained** токен с правом **write** на репозиторий Space `Matveykovich/Kruzchl` (и при необходимости на организацию).

После пуша в ветку `main` workflow выкладывает файлы в [spaces/Matveykovich/Kruzchl](https://huggingface.co/spaces/Matveykovich/Kruzchl).
