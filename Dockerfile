FROM python:3.11-slim

RUN useradd -m -u 1000 user \
    && mkdir -p /data/kruzchl/videos \
    && chown -R user:user /data

WORKDIR /home/user/app

ENV KRUZHCL_DATA=/data/kruzchl
# Secure-cookie по умолчанию определяется по X-Forwarded-Proto (см. main.py).
# Принудительно: KRUZHCL_SECURE_COOKIES=1 или =0

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY --chown=user . .

USER user

EXPOSE 7860

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
