"""개발 서버 띄우기 — DB 부터 확인하고 시작한다.

DB 가 안 떠 있으면 서버는 기동하자마자 죽는다. Docker Desktop 이 꺼지면
컨테이너도 같이 멈추기 때문에 자주 겪는 일인데, 매번 원인을 찾느라
시간을 쓰게 된다. 그래서 순서를 이 스크립트가 대신 챙긴다.

    python run.py              8000 번으로 띄운다
    python run.py --port 8080
"""

import argparse
import socket
import subprocess
import sys
import time

from app.core.config import get_settings

CONTAINER = "outlier-db"


def say(msg: str) -> None:
    print(msg, flush=True)


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def db_reachable(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def ensure_db(host: str, port: int) -> bool:
    if db_reachable(host, port):
        say(f"[OK] DB {host}:{port}")
        return True

    say(f"[..] DB 가 응답하지 않습니다. {CONTAINER} 컨테이너를 켭니다.")
    r = run("docker", "start", CONTAINER)

    if r.returncode != 0:
        err = (r.stderr or "").strip()
        # '없는 컨테이너' 를 먼저 본다. 그 오류문에도 daemon 이라는 낱말이
        # 들어 있어서, 순서를 반대로 두면 엉뚱한 안내가 나간다.
        if "No such container" in err:
            say(f"\n[실패] {CONTAINER} 컨테이너가 없습니다. 처음이라면 이렇게 만듭니다:")
            say("       docker run -d --name outlier-db \\")
            say("         -e POSTGRES_PASSWORD=<비밀번호> -p 5432:5432 \\")
            say("         pgvector/pgvector:pg17")
        elif any(
            k in err
            for k in ("docker API", "daemon is running", "cannot find the file",
                      "dockerDesktop", "Cannot connect to the Docker daemon")
        ):
            say("\n[실패] Docker Desktop 이 꺼져 있습니다.")
            say("       작업표시줄에서 Docker Desktop 을 켜고 고래 아이콘이")
            say("       멈출 때까지 기다린 뒤 다시 실행해주세요.")
        else:
            say(f"\n[실패] docker start 가 실패했습니다:\n       {err[:200]}")
        return False

    # 컨테이너가 떠도 포스트그레스가 받을 준비까지는 몇 초 걸린다.
    for _ in range(20):
        if db_reachable(host, port):
            say(f"[OK] DB {host}:{port}")
            return True
        time.sleep(1)

    say("\n[실패] 컨테이너는 켜졌는데 DB 가 응답하지 않습니다.")
    say(f"       docker logs {CONTAINER} --tail 30 으로 확인해보세요.")
    return False


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-reload", action="store_true",
                    help="코드를 고쳐도 자동 재시작하지 않는다")
    args = ap.parse_args()

    cfg = get_settings()
    if not ensure_db(cfg.postgres_host, cfg.postgres_port):
        return 1

    how = "구글 로그인" if cfg.oauth_ready else (
        "로그인 버튼 한 번 (개발용)" if cfg.dev_login_allowed else "설정 안 됨")
    say(f"[OK] 로그인 방식: {how}")
    say(f"\n  http://127.0.0.1:{args.port}/app   <- 여기로 접속\n")

    cmd = [sys.executable, "-m", "uvicorn", "app.main:app",
           "--port", str(args.port)]
    if not args.no_reload:
        cmd.append("--reload")
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
