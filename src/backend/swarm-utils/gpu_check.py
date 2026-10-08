# The GPUs of the computer: their VRAM, read with nvidia-smi or from the AMD driver, and what they can host.
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path


GPU_TIMEOUT = 10
GPU_REFRESH_SECONDS = 5
MEBIBYTE = 1048576
DRM_FOLDER = Path("/sys/class/drm")
NO_GPU_MESSAGE = "No supported GPU was found (NVIDIA on Linux and Windows, AMD on Linux), so local models cannot run. Choose API models."

gpuCache = {"loaded": 0, "gpus": []}


def bytesToGb(size):
    return round(size / 1000000000, 1)


# nvidia-smi comes with the NVIDIA driver, on Linux and on Windows. On Windows it is not always in the PATH.
def findNvidiaSmi():
    found = shutil.which("nvidia-smi")
    if found:
        return found
    legacy = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"
    return str(legacy) if legacy.exists() else None


# The memory of NVIDIA GPUs, in GB. It never fails: with no tool, no driver, or an answer it cannot read, there is no GPU.
# CREATE_NO_WINDOW (it exists on Windows only) keeps a window from flashing when a user interface asks.
def readNvidiaGpus():
    command = findNvidiaSmi()
    if not command:
        return []
    try:
        result = subprocess.run([command, "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"], capture_output=True, text=True,
                                timeout=GPU_TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in result.stdout.splitlines() if result.returncode == 0 else []:
        try:
            *name, total, free = [part.strip() for part in line.split(",")]
            gpus.append({"name": ", ".join(name), "total": bytesToGb(float(total) * MEBIBYTE), "free": bytesToGb(float(free) * MEBIBYTE)})
        except ValueError:
            continue
    return visibleGpus(gpus)


# The models only use the GPUs that CUDA_VISIBLE_DEVICES lets them see (their numbers, like 0,2), so the others are not counted.
def visibleGpus(gpus):
    visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    if visible is None or not all(part.strip().isdigit() for part in visible.split(",") if part.strip()):
        return gpus
    return [gpus[int(part)] for part in visible.split(",") if part.strip() and int(part) < len(gpus)]


# The memory of AMD GPUs on Linux. The amdgpu driver writes it in files, so no tool is needed.
# Only card0, card1... are read, because the folders of the screen connectors (card0-DP-1...) link to the same device.
def readAmdGpus():
    try:
        cards = sorted(DRM_FOLDER.iterdir())
    except OSError:
        return []
    gpus = []
    for card in cards:
        if not re.fullmatch(r"card\d+", card.name):
            continue
        try:
            total, used = (int((card / "device" / f"mem_info_vram_{kind}").read_text()) for kind in ("total", "used"))
        except (OSError, ValueError):
            continue
        try:
            name = (card / "device" / "product_name").read_text().strip()
        except OSError:
            name = ""
        gpus.append({"name": name or f"AMD GPU {card.name}", "total": bytesToGb(total), "free": bytesToGb(total - used)})
    return gpus


def queryGpus():
    return readNvidiaGpus() + readAmdGpus()


# The GPUs are only read again after a few seconds, so a list of many models can be checked without starting the tool for each one.
def readGpus():
    now = datetime.now().timestamp()
    if now - gpuCache["loaded"] > GPU_REFRESH_SECONDS:
        gpuCache.update(loaded=now, gpus=queryGpus())
    return gpuCache["gpus"]


# What the GPUs can host, in GB. total is all their memory, and free is what no other job uses at this moment, which keeps changing.
# fits says the swarm could run on these GPUs if nothing else used them, and runnable says it can run right now.
def checkVram(needed, gpus):
    total, free = round(sum(gpu["total"] for gpu in gpus), 1), round(sum(gpu["free"] for gpu in gpus), 1)
    used = round(total - free, 1)
    message = ""
    if needed and not gpus:
        message = NO_GPU_MESSAGE
    elif needed > total:
        message = f"The swarm needs about {needed} GB of VRAM but your GPUs only have {total} GB in total."
    elif needed > free:
        message = (f"The swarm needs about {needed} GB of VRAM. Your GPUs have {total} GB in total, but only {free} GB are free now "
                   f"because other jobs use {used} GB. You will not be able to run the swarm until they free enough memory.")
    return {"gpus": gpus, "needed": needed, "total": total, "free": free, "used": used, "left": round(total - needed, 1),
            "fits": needed <= total, "runnable": needed <= free, "message": message}
