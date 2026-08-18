from __future__ import annotations

from dataclasses import dataclass
from collections import deque
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
from threading import Lock, Thread
import time
from uuid import uuid4

from sectvoice.paths import AppPaths


class ASRError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ASRResult:
    text: str
    language: str
    language_probability: float
    segments: tuple[dict[str, object], ...]
    words: tuple[dict[str, object], ...] = ()


class ASRService:
    """Runs one reusable transcription worker outside the Reader process.

    Loading Faster-Whisper for every generated speech window adds several
    seconds of avoidable latency.  The worker keeps the model warm, while this
    service serializes requests and restarts the worker after a crash or a
    timeout.  Reader never imports CTranslate2 into its Qt process.
    """

    _RESULT_PREFIX = "SECTVOICE_ASR_WORKER_RESULT="

    def __init__(
        self,
        paths: AppPaths,
        python_executable: Path,
        script_path: Path,
        model_dir: Path,
        python_paths: tuple[Path, ...] = (),
    ) -> None:
        self.paths = paths
        self.python_executable = python_executable
        self.script_path = script_path
        self.model_dir = model_dir
        self.python_paths = python_paths
        self._lock = Lock()
        self._process: subprocess.Popen[str] | None = None
        self._stdout_lines: Queue[str | None] = Queue()
        self._stderr_tail: deque[str] = deque(maxlen=80)

    @property
    def is_available(self) -> bool:
        return (
            self.python_executable.is_file()
            and self.script_path.is_file()
            and (self.model_dir / "model.bin").is_file()
        )

    def transcribe(
        self,
        audio: Path,
        timeout_seconds: float = 300.0,
        *,
        word_timestamps: bool = False,
    ) -> ASRResult:
        if not self.is_available:
            raise ASRError("自动转写组件或模型尚未安装")
        request_id = uuid4().hex
        deadline = time.monotonic() + timeout_seconds
        with self._lock:
            process = self._ensure_process_locked()
            assert process.stdin is not None
            try:
                process.stdin.write(
                    json.dumps(
                        {
                            "request_id": request_id,
                            "audio": str(audio),
                            "language": "zh",
                            "word_timestamps": word_timestamps,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                detail = self._worker_error_detail_locked()
                self._stop_process_locked()
                raise ASRError(f"自动转写进程不可用：{detail}") from exc

            payload: dict[str, object] | None = None
            while payload is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop_process_locked()
                    raise ASRError("自动转写超时，转写进程已重启")
                try:
                    line = self._stdout_lines.get(timeout=remaining)
                except Empty as exc:
                    self._stop_process_locked()
                    raise ASRError("自动转写超时，转写进程已重启") from exc
                if line is None:
                    detail = self._worker_error_detail_locked()
                    self._stop_process_locked()
                    raise ASRError(f"自动转写进程异常退出：{detail}")
                if not line.startswith(self._RESULT_PREFIX):
                    continue
                candidate = json.loads(line[len(self._RESULT_PREFIX) :])
                if str(candidate.get("request_id") or "") != request_id:
                    continue
                if not bool(candidate.get("ok")):
                    message = str(candidate.get("error") or "未知错误")
                    raise ASRError(f"自动转写失败：{message}")
                raw_result = candidate.get("result")
                if not isinstance(raw_result, dict):
                    raise ASRError("自动转写进程返回了无效结果")
                payload = raw_result

        text = str(payload.get("text") or "").strip()
        if not text:
            raise ASRError("所选片段没有识别到清晰人声，请重新选择")
        return ASRResult(
            text=text,
            language=str(payload.get("language") or "zh"),
            language_probability=float(payload.get("language_probability") or 0),
            segments=tuple(payload.get("segments") or ()),
            words=tuple(payload.get("words") or ()),
        )

    def shutdown(self) -> None:
        with self._lock:
            self._stop_process_locked(graceful=True)

    def _ensure_process_locked(self) -> subprocess.Popen[str]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        self._stop_process_locked()
        environment = os.environ.copy()
        environment.update(self.paths.process_environment())
        if self.python_paths:
            environment["PYTHONHOME"] = str(self.python_executable.parent)
            environment["PYTHONPATH"] = os.pathsep.join(
                str(path) for path in self.python_paths
            )
            environment["PYTHONNOUSERSITE"] = "1"
        command = [
            str(self.python_executable),
            str(self.script_path),
            "--model-dir",
            str(self.model_dir),
        ]
        self._stdout_lines = Queue()
        self._stderr_tail.clear()
        process = subprocess.Popen(
            command,
            cwd=self.script_path.parent,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._process = process
        assert process.stdout is not None
        assert process.stderr is not None
        Thread(
            target=self._read_stdout,
            args=(process, self._stdout_lines),
            name="sectvoice-asr-stdout",
            daemon=True,
        ).start()
        Thread(
            target=self._read_stderr,
            args=(process,),
            name="sectvoice-asr-stderr",
            daemon=True,
        ).start()
        return process

    @staticmethod
    def _read_stdout(
        process: subprocess.Popen[str], lines: Queue[str | None]
    ) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                lines.put(line.rstrip("\r\n"))
        finally:
            lines.put(None)

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        for line in process.stderr:
            self._stderr_tail.append(line.rstrip("\r\n"))

    def _worker_error_detail_locked(self) -> str:
        detail = "\n".join(self._stderr_tail).strip()
        return detail[-4000:] if detail else "没有收到进程诊断信息"

    def _stop_process_locked(self, *, graceful: bool = False) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None and graceful and process.stdin is not None:
            try:
                process.stdin.write('{"command":"shutdown"}\n')
                process.stdin.flush()
                process.wait(timeout=3.0)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3.0)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
