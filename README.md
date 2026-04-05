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

#### Как создать Dataset с нуля

1. Войдите на [huggingface.co](https://huggingface.co) под своим аккаунтом (например **Matveykovich**).
2. Нажмите **+ New** → **Dataset** (или откройте [huggingface.co/new-dataset](https://huggingface.co/new-dataset)).
3. Заполните:
   - **Owner** — ваш пользователь или организация.
   - **Dataset name** — короткое имя латиницей, например `kruzchl-videos` (в итоге репозиторий будет `Matveykovich/kruzchl-videos`).
   - Лицензию можно выбрать любую или оставить по умолчанию.
4. Снимите галочку **Add a README** (если хотите совсем пустой репозиторий) **или** оставьте README — для приложения не важно, папку `kruzhki/` оно создаст само при первой загрузке.
5. Нажмите **Create dataset**.

#### Подключить Dataset к Space

1. Откройте Space → вкладка **Settings** → блок **Variables and secrets**.
2. Добавьте **Secret** `HF_TOKEN` — [токен](https://huggingface.co/settings/tokens) с правом **write** на этот dataset (и чтение).
3. Добавьте **Variable** (не secret) `HF_KRUZHKI_REPO` = `Matveykovich/kruzchl-videos` — **ровно** как в URL репозитория (`ник/имя`).
4. Сохраните и **перезапустите** Space (**Factory reboot** при необходимости).

Файлы появятся в ветке `main` в каталоге `kruzhki/`; рядом — файлы `*.owner` с id сессии.

Если `HF_KRUZHKI_REPO` **не** задан, кружки пишутся только в локальную папку `videos/` на диске Space.

#### «Нет просмотров» после записи

Квота привязана к **cookie-сессии** `kruzhcl_sid`. Сообщение «нет просмотров» чаще всего значит: браузер **не сохранил** cookie или запросы идут **без одной и той же** сессии.

- Откройте Space по **HTTPS** (как даёт Hugging Face), не по смешанному `http://`.
- В инструментах разработчика → **Network**: после **POST /api/upload** должен быть ответ **200** и заголовок **Set-Cookie**; следующий **GET /api/random** должен уходить **с** cookie.
- Убедитесь, что не включён режим «блокировать сторонние cookies» для сайта HF.
- После деплоя обновлённого кода cookie **Secure** выставляется по `X-Forwarded-Proto` / схеме запроса (см. `main.py`), чтобы сессия не терялась за прокси.

Если запись **не дошла** до сервера (ошибка загрузки), счётчик записей не увеличится — смотрите ответ `/api/upload` и логи Space.

#### «Пока нет чужих кружков»

Это **другое** сообщение: квота есть, но в хранилище **нет видео других пользователей** (или вы единственный автор — свои кружки в выдачу не попадают). Нужен хотя бы один кружок от **другой** сессии/пользователя.

## Локальный запуск

```bash
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 7860
```

Откройте http://127.0.0.1:7860

## GitHub → Hugging Face

В настройках репозитория GitHub добавьте секрет `HF_TOKEN` — [Personal Access Token](https://huggingface.co/settings/tokens) или **fine-grained** токен с правом **write** на репозиторий Space `Matveykovich/Kruzchl` (и при необходимости на организацию).

После пуша в ветку `main` workflow выкладывает файлы в [spaces/Matveykovich/Kruzchl](https://huggingface.co/spaces/Matveykovich/Kruzchl).
