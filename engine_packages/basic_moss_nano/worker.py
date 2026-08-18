from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from multiprocessing.connection import Listener
from pathlib import Path
import sys
from threading import Event, Lock, Thread
import traceback
from typing import Any

from runtime_engine import MossNanoEngine, SynthesisCancelled


class Worker:
    def __init__(self, engine: MossNanoEngine, connection: Any) -> None:
        self.engine = engine
        self.connection = connection
        self.send_lock = Lock()
        self.jobs_lock = Lock()
        self.synthesis_lock = Lock()
        self.jobs: dict[tuple[str, int], Event] = {}
        self.shutdown_event = Event()

    def serve(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                message = self.connection.recv()
            except EOFError:
                break
            request_id = str(message.get("request_id") or "")
            command = str(message.get("command") or "")
            try:
                if command == "health":
                    self._send({"request_id": request_id, "type": "response", "ok": True, "engine_loaded": self.engine.runtime is not None, "process_id": os.getpid()})
                elif command == "load":
                    self.engine.load()
                    self._send({"request_id": request_id, "type": "response", "ok": True})
                elif command == "compile":
                    self.engine.load()
                    result = self.engine.compile_voice(
                        Path(message["reference_wav"]), Path(message["destination"])
                    )
                    self._send({"request_id": request_id, "type": "response", "ok": True, "result": result})
                elif command == "synthesize":
                    self.engine.load()
                    Thread(
                        target=self._synthesize,
                        args=(request_id, message),
                        name=f"moss-synthesis-{request_id[:8]}",
                        daemon=True,
                    ).start()
                elif command == "cancel":
                    key = (str(message["session_id"]), int(message["generation_id"]))
                    with self.jobs_lock:
                        event = self.jobs.get(key)
                    if event is not None:
                        event.set()
                    self._send({"request_id": request_id, "type": "response", "ok": True, "cancelled": event is not None})
                elif command == "unload":
                    self.engine.unload()
                    self._send({"request_id": request_id, "type": "response", "ok": True})
                elif command == "shutdown":
                    with self.jobs_lock:
                        for event in self.jobs.values():
                            event.set()
                    self.engine.unload()
                    self._send({"request_id": request_id, "type": "response", "ok": True})
                    self.shutdown_event.set()
                else:
                    raise ValueError(f"unknown engine command: {command}")
            except Exception as exc:
                self._send_error(request_id, exc, message)

    def _synthesize(self, request_id: str, message: dict[str, Any]) -> None:
        session_id = str(message["session_id"])
        generation_id = int(message["generation_id"])
        speech_unit_id = str(message["speech_unit_id"])
        key = (session_id, generation_id)
        cancel_event = Event()
        with self.jobs_lock:
            self.jobs[key] = cancel_event
        sequence = 0

        def emit(
            pcm: bytes,
            sample_rate: int,
            channels: int,
            duration_seconds: float,
            is_pause: bool,
        ) -> None:
            nonlocal sequence
            del is_pause
            self._send(
                {
                    "request_id": request_id,
                    "type": "audio",
                    "session_id": session_id,
                    "generation_id": generation_id,
                    "speech_unit_id": speech_unit_id,
                    "sequence": sequence,
                    "sample_rate": sample_rate,
                    "channels": channels,
                    "sample_format": "f32le",
                    "duration_seconds": duration_seconds,
                    "data": pcm,
                    "is_final": False,
                }
            )
            sequence += 1

        try:
            with self.synthesis_lock:
                if cancel_event.is_set():
                    raise SynthesisCancelled("synthesis was cancelled")
                options = dict(message.get("options") or {})
                result = self.engine.synthesize_stream(
                    text=str(message["text"]),
                    payload_dir=Path(message["payload_path"]),
                    emit=emit,
                    cancel_event=cancel_event,
                    max_text_tokens=int(options.get("max_text_tokens", 75)),
                    seed=options.get("seed"),
                    stable_first_seed=bool(options.get("stable_first_seed", False)),
                )
            self._send(
                {
                    "request_id": request_id,
                    "type": "audio",
                    "session_id": session_id,
                    "generation_id": generation_id,
                    "speech_unit_id": speech_unit_id,
                    "sequence": sequence,
                    "sample_rate": int(result["sample_rate"]),
                    "channels": int(result["channels"]),
                    "sample_format": "f32le",
                    "duration_seconds": 0.0,
                    "data": b"",
                    "is_final": True,
                }
            )
            self._send({"request_id": request_id, "type": "complete", "ok": True, "result": result})
        except SynthesisCancelled:
            self._send({"request_id": request_id, "type": "complete", "ok": True, "cancelled": True})
        except Exception as exc:
            self._send_error(request_id, exc, message)
        finally:
            with self.jobs_lock:
                self.jobs.pop(key, None)

    def _send_error(
        self,
        request_id: str,
        exc: Exception,
        context: dict[str, Any] | None = None,
    ) -> None:
        traceback_text = traceback.format_exc()
        context = context or {}
        text_preview = str(context.get("text") or "")[:160]
        # stderr is redirected by Reader to the package's persistent log file.
        # Keep the record useful for diagnosis without writing payload paths or
        # the local IPC authentication token.
        print(
            json.dumps(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "event": "engine_error",
                    "request_id": request_id,
                    "command": str(context.get("command") or ""),
                    "session_id": str(context.get("session_id") or ""),
                    "generation_id": context.get("generation_id"),
                    "speech_unit_id": str(context.get("speech_unit_id") or ""),
                    "text_preview": text_preview,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback_text,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )
        self._send(
            {
                "request_id": request_id,
                "type": "error",
                "ok": False,
                "error": str(exc),
                "traceback": traceback_text,
            }
        )

    def _send(self, payload: dict[str, Any]) -> None:
        with self.send_lock:
            self.connection.send(payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--auth-token", required=True)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--cpu-threads", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    engine = MossNanoEngine(
        model_dir=args.model_dir,
        output_dir=args.output_dir,
        cpu_threads=args.cpu_threads,
    )
    with Listener((args.host, args.port), authkey=bytes.fromhex(args.auth_token)) as listener:
        connection = listener.accept()
        try:
            Worker(engine, connection).serve()
        finally:
            connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
