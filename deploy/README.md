# 서버에 올리기 (네이버 클라우드, 서버 1대)

노트북에서 돌던 것을 그대로 서버 한 대에 올린다. 컨테이너 셋: DB · 앱 · Caddy(https).
올리면 매시간 감시와 매일 파기가 노트북 없이 돈다.

## 0. 콘솔에서 (사람이)
1. Server 생성 — Ubuntu 22.04, 2 vCPU / 4 GB, 디스크 50 GB. 인증키 새로 만들어 .pem 저장.
2. ACG 에 22 / 80 / 443 인바운드 허용.
3. 공인 IP 신청 → 이 서버에 연결.

## 1. 서버 준비 (한 번)
```bash
ssh -i 키.pem root@공인IP
apt-get update && apt-get install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sh
git clone https://github.com/tmdals5512/youtube-comment-moderator.git /srv/outlier
cd /srv/outlier && git checkout feat/moderation-pipeline
cp deploy/.env.example deploy/.env && nano deploy/.env     # 비밀값 채우기
```

## 2. DB 먼저 올리고 노트북 데이터 넣기 (한 번)
`scripts/init_db.py` 는 기존 테이블에 컬럼을 더하는 식이라 **빈 DB 에서는 앱이 못 뜬다.**
그래서 DB 만 먼저 올리고 노트북 덤프를 넣은 뒤 앱을 올린다. (2026-09-17 실제로 그렇게 됐다.)

노트북에서:
```bash
docker exec outlier-db pg_dump -U postgres -Fc postgres > outlier.dump
scp -i 키.pem outlier.dump root@공인IP:/srv/outlier/
```
서버에서:
```bash
cd /srv/outlier/deploy
docker compose up -d db
docker compose cp ../outlier.dump db:/tmp/outlier.dump
docker compose exec -T db pg_restore -U postgres -d postgres --no-owner /tmp/outlier.dump
```

## 3. 앱 올리기
```bash
docker compose up -d --build
docker compose ps                  # 셋 다 Up
curl -s localhost/api/health/watch # enabled: true
```

## 주의 — .env 값은 한 줄로
`KEY="` 다음 줄에 값이 이어지는 두 줄짜리 값은 노트북(python-dotenv)에선 읽히지만
docker compose 는 "unterminated quoted value" 로 거부한다. 전부 `KEY=값` 한 줄로.
2026-09-17 에 이것 때문에 키가 채팅에 노출돼 재발급했다.

## 4. 구글 콘솔
OAuth 클라이언트의 승인된 리디렉션 URI 에 `https://도메인/api/auth/callback` 과
`https://도메인/api/channels/connect/callback` 추가. 도메인 없이 IP 만 있으면 구글 로그인은
밖에서 안 된다 (구글이 http 를 localhost 에만 허용). 그때까지 로그인은 노트북에서.

## 5. 도메인이 생기면
`deploy/.env` 의 `SITE_ADDRESS=도메인`, `OAUTH_REDIRECT_BASE=https://도메인`,
`SESSION_COOKIE_SECURE=true` 로 바꾸고 `docker compose up -d`. Caddy 가 인증서를 받는다.

## 코드 바꿨을 때
```bash
cd /srv/outlier && git pull && cd deploy && docker compose up -d --build
```

## 알아둘 것
- DB 는 바깥에 포트를 열지 않는다. 앱 컨테이너만 접속한다.
- `/label` 은 `LABEL_KEY` 로 막혀 있다. 팀에만 알려준다.
- 크레딧: 이 구성은 서버 1대 + 공인 IP 1개다. 다른 리소스를 만들지 않는다.
