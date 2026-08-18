from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys

import psutil
from PySide6.QtMultimedia import QMediaDevices

from sectvoice.core.media import FFmpegProcessor
from sectvoice.core.model_packages import EnginePackageRecord, ModelPackageManager
from sectvoice.domain import Tier
from sectvoice.paths import AppPaths


@dataclass(frozen=True, slots=True)
class DiagnosticItem:
    name: str
    passed: bool
    detail: str
    required: bool = True


def run_startup_diagnostics(
    paths: AppPaths,
    ffmpeg: FFmpegProcessor,
    packages: ModelPackageManager,
    asr_available: bool,
) -> tuple[DiagnosticItem, ...]:
    items: list[DiagnosticItem] = []
    items.append(
        DiagnosticItem(
            "Python环境",
            sys.version_info[:2] == (3, 10),
            f"{sys.executable} · {sys.version.split()[0]}",
        )
    )
    try:
        paths.ensure_writable_directories()
        items.append(DiagnosticItem("目录可写", True, str(paths.root)))
    except Exception as exc:
        items.append(DiagnosticItem("目录可写", False, str(exc)))
    try:
        items.append(DiagnosticItem("FFmpeg", True, ffmpeg.health_check()))
    except Exception as exc:
        items.append(DiagnosticItem("FFmpeg", False, str(exc)))
    memory = psutil.virtual_memory()
    items.append(
        DiagnosticItem(
            "系统内存",
            memory.total >= 8 * 1024**3,
            f"总计 {memory.total / 1024**3:.1f}GB，可用 {memory.available / 1024**3:.1f}GB",
        )
    )
    audio = QMediaDevices.defaultAudioOutput()
    items.append(
        DiagnosticItem(
            "音频输出设备",
            not audio.isNull(),
            audio.description() if not audio.isNull() else "未检测到输出设备",
        )
    )
    basic = packages.active_for(Tier.BASIC)
    standard = packages.active_for(Tier.STANDARD)
    gpu_ok, gpu_detail = _gpu_status(paths, standard)
    items.append(DiagnosticItem("CUDA/GPU", gpu_ok, gpu_detail, required=False))
    items.append(
        DiagnosticItem(
            "基础语音包",
            basic is not None,
            basic.engine_id + " " + basic.engine_version if basic else "未安装/未通过smoke",
            required=False,
        )
    )
    items.append(
        DiagnosticItem(
            "中级语音包",
            standard is not None,
            standard.engine_id + " " + standard.engine_version if standard else "未安装/未通过smoke",
            required=False,
        )
    )
    items.append(
        DiagnosticItem(
            "自动转写",
            asr_available,
            "Faster-Whisper Small CPU int8" if asr_available else "组件或模型未安装",
        )
    )
    return tuple(items)


def _gpu_status(
    paths: AppPaths, standard: EnginePackageRecord | None
) -> tuple[bool, str]:
    if standard is None:
        return False, "未安装或未启用中级语音包；基础包可用CPU运行"
    portable_python = paths.runtime / "common" / "python310" / "python.exe"
    engine_python = standard.runtime_path / ".venv" / "Scripts" / "python.exe"
    python = portable_python if portable_python.is_file() else engine_python
    if not python.is_file():
        return False, "中级引擎环境未安装；基础包可用CPU运行"
    environment = paths.process_environment()
    if python == portable_python:
        environment["PYTHONHOME"] = str(portable_python.parent)
        environment["PYTHONPATH"] = str(
            standard.runtime_path / ".venv" / "Lib" / "site-packages"
        )
        environment["PYTHONNOUSERSITE"] = "1"
    try:
        result = subprocess.run(
            [
                str(python),
                "-c",
                "import torch; print(torch.cuda.is_available()); "
                "print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CUDA unavailable'); "
                "print(round(torch.cuda.get_device_properties(0).total_memory/1024**3,2) if torch.cuda.is_available() else 0)",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        lines = result.stdout.decode("utf-8", errors="replace").splitlines()
        ok = result.returncode == 0 and lines and lines[0].strip() == "True"
        return ok, " · ".join(lines[-3:])
    except Exception as exc:
        return False, str(exc)
