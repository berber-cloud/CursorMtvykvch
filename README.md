---
title: Kruzhcl
emoji: 📹
colorFrom: gray
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# Kruzhcl

Круглые видеосообщения («кружки»): запись в браузере и просмотр случайных кружков других пользователей. **1 запись = 5 просмотров** чужих видео.

Файлы хранятся на диске Space (для сохранения между перезапусками включите [persistent storage](https://huggingface.co/docs/hub/spaces-storage) и используйте каталог `/data`).

## Локальный запуск

```bash
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 7860
```

Откройте http://127.0.0.1:7860

## GitHub → Hugging Face

В настройках репозитория GitHub добавьте секрет `HF_TOKEN` — токен Hugging Face с правом **write** для репозитория Space.

После пуша в ветку `main` workflow выкладывает файлы в `spaces/Matveykovich/kruzhcl`.
