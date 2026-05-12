"""현재 띄워진 RunPod 인스턴스 목록."""
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
import runpod
runpod.api_key = os.environ["RUNPOD_API_KEY"]
pods = runpod.get_pods()
print(f"pod 수: {len(pods)}")
for p in pods:
    pid = p.get("id")
    name = p.get("name", "")
    status = p.get("desiredStatus")
    machine = p.get("machine", {}) or {}
    gpu_name = machine.get("gpuDisplayName", "?")
    cost = p.get("costPerHr") or 0
    runtime = p.get("runtime") or {}
    uptime = runtime.get("uptimeInSeconds", 0)
    print(f"  id={pid}  name={name}  status={status}  gpu={gpu_name}  cost=${cost:.3f}/h  uptime={uptime}s")
