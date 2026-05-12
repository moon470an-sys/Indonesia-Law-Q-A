"""
RunPod GPU 인스턴스 spawn → 데이터 업로드 → BGE-M3 임베딩 → 결과 다운로드 → 종료.

전체 흐름 자동화. 실패 시 자동 cleanup (인스턴스 terminate).

실행:
    python scripts/runpod_orchestrate.py \\
        --jsonl D:\\rag_data\\chunks_v2.jsonl \\
        --out-dir D:\\rag_data\\embeddings
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# 프로젝트 .env 로드
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import runpod
import paramiko
from scp import SCPClient


# === 설정 ===
# GPU fallback 순서: 저렴 + 가용성 높은 순. BGE-M3에는 16GB 이상이면 충분.
GPU_FALLBACK = [
    "NVIDIA GeForce RTX 3090",        # 24GB, 가장 저렴 ~$0.22/h
    "NVIDIA GeForce RTX 4090",        # 24GB, ~$0.69/h
    "NVIDIA GeForce RTX 4080 SUPER",  # 16GB
    "NVIDIA GeForce RTX 4080",        # 16GB
    "NVIDIA L4",                      # 24GB
    "NVIDIA RTX A5000",               # 24GB
    "NVIDIA A40",                     # 48GB
]
GPU_TYPE_ID = "NVIDIA GeForce RTX 4090"
IMAGE_NAME = "runpod/pytorch:2.1.0-py3.10-cuda11.8.0-devel-ubuntu22.04"
CONTAINER_DISK_GB = 50
POD_NAME = "indonesia-rag-embed"
CLOUD_TYPE = "ALL"
BID_PER_GPU = 0.40         # (legacy 인자, 1.9.0 SDK에서 무시됨)

SSH_USERNAME = "root"
SSH_KEY_PATH = Path.home() / ".ssh" / "id_ed25519"

REMOTE_WORKDIR = "/workspace"
REMOTE_EMBED_SCRIPT = "/workspace/embed_runpod.py"


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def wait_for_pod_ready(pod_id: str, timeout: int = 600) -> dict:
    """Pod가 RUNNING + ports 할당까지 polling."""
    log(f"  pod {pod_id} ready 대기 (timeout {timeout}s)")
    t_start = time.time()
    while time.time() - t_start < timeout:
        pod = runpod.get_pod(pod_id)
        # RunPod SDK 응답 구조: { id, ..., runtime: { ports: [{...}], uptimeInSeconds: ... }, desiredStatus: "RUNNING" }
        desired = pod.get("desiredStatus")
        runtime = pod.get("runtime") or {}
        ports = runtime.get("ports") or []
        ssh_ports = [p for p in ports if p.get("privatePort") == 22 and p.get("ip") and p.get("publicPort")]
        if desired == "RUNNING" and ssh_ports:
            ssh = ssh_ports[0]
            log(f"  ready! ssh={ssh['ip']}:{ssh['publicPort']}")
            return {"pod": pod, "ssh_ip": ssh["ip"], "ssh_port": ssh["publicPort"]}
        log(f"  ... desired={desired}, ports={len(ports)} (waited {int(time.time()-t_start)}s)")
        time.sleep(10)
    raise TimeoutError(f"pod {pod_id} did not become ready in {timeout}s")


def ssh_connect(ip: str, port: int, key_path: Path, retries: int = 12) -> paramiko.SSHClient:
    """SSH connection with retry (RunPod sshd가 뜨는 데 시간 걸림)."""
    log(f"  SSH connect → {ip}:{port}")
    pkey = paramiko.Ed25519Key.from_private_key_file(str(key_path))
    last_exc: Exception | None = None
    for i in range(retries):
        try:
            cli = paramiko.SSHClient()
            cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            cli.connect(
                hostname=ip, port=port, username=SSH_USERNAME,
                pkey=pkey, timeout=10, banner_timeout=10, auth_timeout=10,
            )
            log("  SSH OK")
            return cli
        except Exception as exc:
            last_exc = exc
            log(f"  SSH attempt {i+1}/{retries} failed: {type(exc).__name__}: {exc}")
            time.sleep(10)
    raise RuntimeError(f"SSH 연결 실패: {last_exc}")


def run_cmd(ssh: paramiko.SSHClient, cmd: str, label: str = "", stream: bool = True) -> int:
    """SSH로 명령 실행. stream=True면 실시간 출력."""
    if label:
        log(f"  $ {label}")
    stdin, stdout, stderr = ssh.exec_command(cmd, get_pty=True)
    if stream:
        for line in iter(stdout.readline, ""):
            line = line.rstrip()
            if line:
                print(f"    {line}", flush=True)
    exit_code = stdout.channel.recv_exit_status()
    if exit_code != 0 and not stream:
        err = stderr.read().decode("utf-8", errors="replace")
        print(f"    STDERR: {err}", flush=True)
    return exit_code


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--gpu-type", default=GPU_TYPE_ID)
    ap.add_argument("--image", default=IMAGE_NAME)
    ap.add_argument("--bid", type=float, default=BID_PER_GPU)
    ap.add_argument("--cloud-type", default=CLOUD_TYPE,
                    choices=["COMMUNITY", "SECURE", "ALL"])
    ap.add_argument("--container-disk-gb", type=int, default=CONTAINER_DISK_GB)
    ap.add_argument("--terminate-on-finish", action="store_true", default=True)
    ap.add_argument("--keep-on-error", action="store_true",
                    help="에러 시 debug 위해 인스턴스 유지")
    args = ap.parse_args()

    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        log("FATAL: RUNPOD_API_KEY env var 없음 (.env 로드 확인)")
        return 1
    runpod.api_key = api_key

    jsonl_path = Path(args.jsonl)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not jsonl_path.exists():
        log(f"FATAL: 입력 JSONL 없음: {jsonl_path}")
        return 1

    if not SSH_KEY_PATH.exists():
        log(f"FATAL: SSH key 없음: {SSH_KEY_PATH}")
        return 1

    file_size_mb = jsonl_path.stat().st_size / 1024 / 1024
    log(f"=== 임베딩 작업 시작 ===")
    log(f"  입력: {jsonl_path} ({file_size_mb:.1f} MB)")
    log(f"  출력: {out_dir}")
    log(f"  GPU: {args.gpu_type}, image: {args.image}")
    log(f"  cloud: {args.cloud_type}, bid: ${args.bid}/h")

    # === 1) Pod spawn (fallback 순서로 GPU 시도) ===
    log("[1/7] Pod spawn (GPU fallback 순서)")
    gpu_candidates = [args.gpu_type] + [g for g in GPU_FALLBACK if g != args.gpu_type]
    pod = None
    last_err: Exception | None = None
    for gpu_id in gpu_candidates:
        log(f"  시도: {gpu_id}")
        try:
            pod = runpod.create_pod(
                name=POD_NAME,
                image_name=args.image,
                gpu_type_id=gpu_id,
                cloud_type=args.cloud_type,
                gpu_count=1,
                volume_in_gb=0,
                container_disk_in_gb=args.container_disk_gb,
                ports="22/tcp",
                start_ssh=True,
                support_public_ip=True,
            )
            log(f"  성공: {gpu_id}")
            break
        except Exception as exc:
            last_err = exc
            log(f"  실패: {type(exc).__name__}: {exc}")
            time.sleep(2)
    if pod is None:
        raise RuntimeError(f"모든 GPU 후보 spawn 실패. 마지막 에러: {last_err}")

    pod_id = pod["id"]
    log(f"  pod created: id={pod_id}")

    ssh: paramiko.SSHClient | None = None
    try:
        # === 2) Ready 대기 ===
        log("[2/7] Pod ready 대기...")
        info = wait_for_pod_ready(pod_id, timeout=600)
        ssh_ip = info["ssh_ip"]
        ssh_port = info["ssh_port"]

        # SSHD 부팅 시간 추가 대기
        log("  SSHD warm-up (30s)...")
        time.sleep(30)

        # === 3) SSH 연결 ===
        log("[3/7] SSH 연결")
        ssh = ssh_connect(ssh_ip, ssh_port, SSH_KEY_PATH)

        # === 4) 데이터 업로드 ===
        log("[4/7] 데이터 업로드")
        log(f"  scp: chunks_v2.jsonl ({file_size_mb:.1f} MB) → {REMOTE_WORKDIR}/")
        t_up = time.time()
        with SCPClient(ssh.get_transport(), socket_timeout=60.0) as scp:
            scp.put(str(jsonl_path), f"{REMOTE_WORKDIR}/chunks_v2.jsonl")
        log(f"  업로드 OK ({time.time()-t_up:.0f}s)")

        # embed_runpod.py 업로드
        embed_script_local = ROOT / "scripts" / "embed_runpod.py"
        if not embed_script_local.exists():
            raise FileNotFoundError(f"{embed_script_local} 없음")
        with SCPClient(ssh.get_transport()) as scp:
            scp.put(str(embed_script_local), REMOTE_EMBED_SCRIPT)
        log(f"  embed script 업로드 OK")

        # === 5) Setup + 임베딩 실행 ===
        log("[5/7] 환경 setup + 임베딩 실행")
        # FlagEmbedding 설치
        rc = run_cmd(ssh,
            "pip install --quiet --upgrade pip && "
            "pip install --quiet FlagEmbedding peft && "
            "python -c 'import torch; print(\"torch:\", torch.__version__, \"cuda:\", torch.cuda.is_available(), \"gpu:\", torch.cuda.get_device_name(0))'",
            label="pip install + cuda check",
        )
        if rc != 0:
            raise RuntimeError(f"setup 실패 (rc={rc})")

        # 임베딩 실행
        rc = run_cmd(ssh,
            f"cd {REMOTE_WORKDIR} && "
            f"python embed_runpod.py "
            f"--jsonl {REMOTE_WORKDIR}/chunks_v2.jsonl "
            f"--out-dir {REMOTE_WORKDIR}/embeddings "
            f"--batch-size 128 --max-length 512 --device cuda --fp16",
            label="BGE-M3 임베딩 실행",
        )
        if rc != 0:
            raise RuntimeError(f"임베딩 실패 (rc={rc})")

        # === 6) 결과 다운로드 ===
        log("[6/7] 결과 다운로드")
        remote_files = [
            f"{REMOTE_WORKDIR}/embeddings/chunks_v2.embeddings.npy",
            f"{REMOTE_WORKDIR}/embeddings/chunks_v2.ids.txt",
            f"{REMOTE_WORKDIR}/embeddings/chunks_v2.embeddings.meta.json",
        ]
        with SCPClient(ssh.get_transport(), socket_timeout=120.0) as scp:
            for rf in remote_files:
                local_name = Path(rf).name
                local_path = out_dir / local_name
                log(f"  scp {rf} → {local_path}")
                scp.get(rf, str(local_path))

        log("  다운로드 OK")
        log(f"=== 완료 ===")
        success = True
    except Exception as exc:
        log(f"ERROR: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        success = False
    finally:
        if ssh:
            try:
                ssh.close()
            except Exception:
                pass
        # === 7) 인스턴스 종료 ===
        if success or args.terminate_on_finish and not args.keep_on_error:
            log(f"[7/7] Pod terminate: {pod_id}")
            try:
                runpod.terminate_pod(pod_id)
                log(f"  terminated.")
            except Exception as exc:
                log(f"  WARN: terminate 실패: {exc}")
                log(f"  → RunPod 콘솔에서 수동 종료 필요: {pod_id}")
        else:
            log(f"  pod 유지 (debug). 수동 종료 필요: {pod_id}")

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
