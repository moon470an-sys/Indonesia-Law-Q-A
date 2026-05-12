"""List RunPod GPU types."""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
import runpod
runpod.api_key = os.environ["RUNPOD_API_KEY"]

gpus = runpod.get_gpus()
keep_keywords = [
    "RTX 4090", "RTX 5090", "RTX 4080", "RTX A6000", "RTX A5000", "RTX A4000",
    "L4", "L40", "A40", "A100", "H100", "RTX 3090", "RTX 6000",
]
for g in sorted(gpus, key=lambda x: x.get("memoryInGb", 0)):
    name = g.get("displayName", "")
    if any(k in name for k in keep_keywords):
        print(f"  id={g['id']:<35} name={name:<35} mem={g.get('memoryInGb')}GB")
