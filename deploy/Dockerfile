# 앱 컨테이너. 서버 한 대에 DB · 앱 · https(Caddy) 세 컨테이너로 올린다.
# 빌드 문맥은 레포 루트다 (docker-compose.yml 의 context: ..).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY docs ./docs

# 테이블은 전부 CREATE IF NOT EXISTS 라 매 기동마다 돌려도 안전하다.
# 그 다음 서버. --proxy-headers: Caddy 뒤에 있으니 원래 주소(https)를 믿는다.
CMD ["sh", "-c", "python -m scripts.init_db && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
