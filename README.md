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

Файлы хранятся на диске Space (для сохранения между перезапусками включите [persistent storage](https://huggingface.co/docs/hub/spaces-storage) и используйте каталог `/data`).

## Локальный запуск

```bash
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 7860
```

Откройте http://127.0.0.1:7860

## GitHub → Hugging Face

В настройках репозитория GitHub добавьте секрет `HF_TOKEN` — [Personal Access Token](https://huggingface.co/settings/tokens) или **fine-grained** токен с правом **write** на репозиторий Space `Matveykovich/Kruzchl` (и при необходимости на организацию).

После пуша в ветку `main` workflow выкладывает файлы в [spaces/Matveykovich/Kruzchl](https://huggingface.co/spaces/Matveykovich/Kruzchl).
