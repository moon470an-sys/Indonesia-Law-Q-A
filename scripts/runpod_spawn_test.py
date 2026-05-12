"""GPU spawn 단위 테스트 — fallback 후보 중 어느 게 실제 가용한지 확인.

성공 시 즉시 terminate. 가용 GPU 후보만 출력.
"""
import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
import runpod
runpod.api_key = os.environ["RUNPOD_API_KEY"]

CANDIDATES = [
    "NVIDIA GeForce RTX 3090",
    "NVIDIA GeForce RTX 4090",
    "NVIDIA GeForce RTX 4080 SUPER",
    "NVIDIA GeForce RTX 4080",
    "NVIDIA L4",
    "NVIDIA RTX A5000",
    "NVIDIA A40",
    "NVIDIA RTX A4000",
    "NVIDIA GeForce RTX 5090",
    "NVIDIA RTX A6000",
    "NVIDIA L40S",
]

IMAGE = os.environ.get("TEST_IMAGE", "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04")

for gpu_id in CANDIDATES:
    print(f"\n>>> 시도: {gpu_id}")
    try:
        pod = runpod.create_pod(
            name="spawn-test",
            image_name=IMAGE,
            gpu_type_id=gpu_id,
            cloud_type="ALL",
            gpu_count=1,
            volume_in_gb=0,
            container_disk_in_gb=50,
            ports="22/tcp",
            start_ssh=True,
            support_public_ip=True,
        )
        pod_id = pod["id"]
        print(f"  OK 성공! pod_id={pod_id}")
        try:
            runpod.terminate_pod(pod_id)
            print(f"  OK terminate OK")
        except Exception as e:
            print(f"  WARN: terminate 실패: {e}")
        print(f"\n=== AVAILABLE GPU: {gpu_id} ===")
        sys.exit(0)
    except Exception as exc:
        msg = str(exc)
        if "resources" in msg.lower() or "something went wrong" in msg.lower():
            print(f"  X 가용성 부족")
        else:
            print(f"  X {type(exc).__name__}: {msg[:200]}")
        time.sleep(1)

print("\n=== ALL CANDIDATES FAILED ===")
sys.exit(1)
