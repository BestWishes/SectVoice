from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from multiprocessing.connection import Client
import os
from pathlib import Path
from queue import Empty, Queue
import secrets
import socket
import subprocess
from threading import Event, Lock, Thread
import time
from typing import Any
from uuid import UUID, uuid4

import psutil

from sectvoice.domain import AudioChunk, PCMFormat


class EngineProcessError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EngineProcessSpec:
    python_executable: Path
    worker_script: Path
    working_directory: Path
    environment: dict[str, str]
    log_path: Path | None = None
    startup_timeout_seconds: float = 90.0


class EngineProcessClient:
    """Reader-side IPC client for one isolated engine package process."""

    def __init__(self, spec: EngineProcessSpec) -> None:
        self.spec = spec
        self._process: subprocess.Popen[bytes] | None = None
        self._connection: Any = None
        self._send_lock = Lock()
        self._queues_lock = Lock()
        self._queues: dict[str, Queue[dict[str, Any]]] = {}
        self._receiver: Thread | None = None
        self._closed = Event()
        self._log_handle: Any = None
        self._worker_pid: int | None = None

    @property
    def is_running(self) -> bool:
        if self._closed.is_set() or self._process is None:
            return False
        if self._worker_pid is not None:
            return psutil.pid_exists(self._worker_pid)
        return self._process.poll() is None

    @property
    def process_id(self) -> int | None:
        if not self.is_running:
            return None
        return self._worker_pid or (self._process.pid if self._process is not None else None)

    @property
    def process_tree_rss_mib(self) -> float:
        """Return the resident memory owned by this isolated engine process tree."""

        process_id = self.process_id
        if process_id is None:
            return 0.0
        try:
            root = psutil.Process(process_id)
            processes = (root, *root.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0.0
        resident_bytes = 0
        for process in processes:
            try:
                resident_bytes += process.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return resident_bytes / 1024**2

    def start(self, extra_args: list[str] | None = None) -> None:
        if self.is_running:
            return
        self._closed.clear()
        port = self._reserve_local_port()
        auth_token = secrets.token_hex(32)
        command = [
            str(self.spec.python_executable),
            str(self.spec.worker_script),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--auth-token",
            auth_token,
            *(extra_args or []),
        ]
        environment = os.environ.copy()
        environment.update(self.spec.environment)
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        output_target: Any = subprocess.DEVNULL
        if self.spec.log_path is not None:
            self.spec.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = self.spec.log_path.open("ab", buffering=0)
            output_target = self._log_handle
        self._process = subprocess.Popen(
            command,
            cwd=self.spec.working_directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=output_target,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
        )
        deadline = time.monotonic() + self.spec.startup_timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise EngineProcessError(self._process_failure_message("engine exited during startup"))
            try:
                self._connection = Client(("127.0.0.1", port), authkey=bytes.fromhex(auth_token))
                break
            except (ConnectionRefusedError, OSError) as exc:
                last_error = exc
                time.sleep(0.05)
        else:
            self.stop(force=True)
            raise EngineProcessError(f"engine startup timed out: {last_error}")

        self._receiver = Thread(target=self._receive_loop, name="sectvoice-engine-receiver", daemon=True)
        self._receiver.start()
        response = self.request("health", timeout_seconds=15.0)
        if not response.get("ok"):
            raise EngineProcessError(str(response.get("error") or "engine health check failed"))
        self._worker_pid = int(response.get("process_id") or self._process.pid)

    def request(
        self, command: str, timeout_seconds: float = 60.0, **payload: Any
    ) -> dict[str, Any]:
        request_id = uuid4().hex
        queue = self._register_queue(request_id)
        try:
            self._send({"command": command, "request_id": request_id, **payload})
            try:
                message = queue.get(timeout=timeout_seconds)
            except Empty as exc:
                raise EngineProcessError(f"engine command timed out: {command}") from exc
            if message.get("type") == "error" or not message.get("ok", True):
                raise EngineProcessError(str(message.get("error") or f"engine command failed: {command}"))
            return message
        finally:
            self._unregister_queue(request_id)

    def stream_synthesis(
        self,
        *,
        session_id: UUID,
        generation_id: int,
        speech_unit_id: UUID,
        text: str,
        payload_path: Path,
        timeout_seconds: float = 120.0,
        options: dict[str, Any] | None = None,
    ) -> Iterator[AudioChunk]:
        request_id = uuid4().hex
        queue = self._register_queue(request_id)
        self._send(
            {
                "command": "synthesize",
                "request_id": request_id,
                "session_id": str(session_id),
                "generation_id": generation_id,
                "speech_unit_id": str(speech_unit_id),
                "text": text,
                "payload_path": str(payload_path),
                "options": options or {},
            }
        )
        try:
            while True:
                try:
                    message = queue.get(timeout=timeout_seconds)
                except Empty as exc:
                    raise EngineProcessError("engine synthesis stream timed out") from exc
                message_type = message.get("type")
                if message_type == "error":
                    raise EngineProcessError(str(message.get("error") or "engine synthesis failed"))
                if message_type == "complete":
                    break
                if message_type != "audio":
                    continue
                yield AudioChunk(
                    session_id=UUID(message["session_id"]),
                    generation_id=int(message["generation_id"]),
                    sequence=int(message["sequence"]),
                    speech_unit_id=UUID(message["speech_unit_id"]),
                    pcm_format=PCMFormat(
                        sample_rate=int(message["sample_rate"]),
                        channels=int(message["channels"]),
                        sample_format=str(message["sample_format"]),
                    ),
                    duration_seconds=float(message["duration_seconds"]),
                    data=message["data"],
                    is_final=bool(message.get("is_final", False)),
                )
        finally:
            self._unregister_queue(request_id)

    def cancel(self, session_id: UUID, generation_id: int) -> None:
        self.request(
            "cancel",
            timeout_seconds=5.0,
            session_id=str(session_id),
            generation_id=generation_id,
        )

    def stop(self, force: bool = False) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None and not force and self._connection is not None:
            try:
                self.request("shutdown", timeout_seconds=5.0)
            except Exception:
                force = True
        if force:
            self._kill_process_tree()
        elif process.poll() is None:
            process.terminate()
        if process.poll() is None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        # A Windows venv launcher may exit while the real interpreter it
        # spawned remains alive.  The worker PID comes from the IPC health
        # response, so always perform one final tree cleanup before forgetting
        # either PID.  At this point shutdown has already been requested and no
        # synthesis stream may still be accepted.
        self._kill_process_tree()
        self._closed.set()
        try:
            if self._connection is not None:
                self._connection.close()
        finally:
            self._connection = None
            self._process = None
            self._worker_pid = None
            if self._log_handle is not None:
                self._log_handle.close()
                self._log_handle = None

    def _kill_process_tree(self) -> None:
        launcher_pid = (
            self._process.pid
            if self._process is not None and self._process.poll() is None
            else None
        )
        pids = {pid for pid in (self._worker_pid, launcher_pid) if pid}
        processes: list[psutil.Process] = []
        for pid in pids:
            try:
                process = psutil.Process(pid)
                processes.extend(process.children(recursive=True))
                processes.append(process)
            except psutil.NoSuchProcess:
                continue
        for process in reversed(processes):
            try:
                process.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    def _send(self, message: dict[str, Any]) -> None:
        if not self.is_running or self._connection is None:
            raise EngineProcessError("engine process is not connected")
        with self._send_lock:
            self._connection.send(message)

    def _register_queue(self, request_id: str) -> Queue[dict[str, Any]]:
        queue: Queue[dict[str, Any]] = Queue()
        with self._queues_lock:
            self._queues[request_id] = queue
        return queue

    def _unregister_queue(self, request_id: str) -> None:
        with self._queues_lock:
            self._queues.pop(request_id, None)

    def _receive_loop(self) -> None:
        failure: str | None = None
        try:
            while not self._closed.is_set():
                message = self._connection.recv()
                request_id = str(message.get("request_id") or "")
                with self._queues_lock:
                    queue = self._queues.get(request_id)
                if queue is not None:
                    queue.put(message)
        except (EOFError, OSError, BrokenPipeError) as exc:
            failure = self._process_failure_message(str(exc))
        finally:
            self._closed.set()
            if failure:
                with self._queues_lock:
                    queues = tuple(self._queues.values())
                for queue in queues:
                    queue.put({"type": "error", "ok": False, "error": failure})

    def _process_failure_message(self, prefix: str) -> str:
        log_tail = ""
        if self.spec.log_path is not None and self.spec.log_path.exists():
            try:
                log_tail = self.spec.log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            except Exception:
                log_tail = ""
        return f"{prefix}{': ' + log_tail if log_tail else ''}"

    @staticmethod
    def _reserve_local_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])
