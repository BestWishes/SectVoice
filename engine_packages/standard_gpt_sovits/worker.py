from __future__ import annotations

import argparse
import os
from multiprocessing.connection import Listener
from pathlib import Path
import sys
from threading import Event, Lock, Thread
import traceback
from typing import Any

from runtime_engine import (
    DEFAULT_GPU_COMPUTE_PACING,
    GptSovitsEngine,
    SynthesisCancelled,
)


class Worker:
    def __init__(self, engine: GptSovitsEngine, connection: Any) -> None:
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
                    self._send({"request_id": request_id, "type": "response", "ok": True, "engine_loaded": self.engine.tts is not None, "process_id": os.getpid()})
                elif command == "load":
                    self.engine.load()
                    self._send({"request_id": request_id, "type": "response", "ok": True})
                elif command == "compile":
                    self.engine.load()
                    result = self.engine.compile_voice(
                        Path(message["reference_wav"]),
                        str(message["reference_transcript"]),
                        str(message.get("language") or "zh-CN"),
                        Path(message["destination"]),
                    )
                    self._send({"request_id": request_id, "type": "response", "ok": True, "result": result})
                elif command == "synthesize":
                    self.engine.load()
                    Thread(target=self._synthesize, args=(request_id, message), daemon=True).start()
                elif command == "cancel":
                    key = (str(message["session_id"]), int(message["generation_id"]))
                    with self.jobs_lock:
                        event = self.jobs.get(key)
                    if event is not None:
                        event.set()
                        self.engine.request_cancel()
                    self._send({"request_id": request_id, "type": "response", "ok": True, "cancelled": event is not None})
                elif command == "unload":
                    self.engine.unload()
                    self._send({"request_id": request_id, "type": "response", "ok": True})
                elif command == "shutdown":
                    with self.jobs_lock:
                        for event in self.jobs.values():
                            event.set()
                    self.engine.request_cancel()
                    self.engine.unload()
                    self._send({"request_id": request_id, "type": "response", "ok": True})
                    self.shutdown_event.set()
                else:
                    raise ValueError(f"unknown engine command: {command}")
            except Exception as exc:
                self._send_error(request_id, exc)

    def _synthesize(self, request_id: str, message: dict[str, Any]) -> None:
        session_id = str(message["session_id"])
        generation_id = int(message["generation_id"])
        speech_unit_id = str(message["speech_unit_id"])
        key = (session_id, generation_id)
        cancel_event = Event()
        with self.jobs_lock:
            self.jobs[key] = cancel_event
        sequence = 0

        def emit(pcm: bytes, sample_rate: int, channels: int, duration_seconds: float, is_pause: bool) -> None:
            nonlocal sequence
            del is_pause
            if cancel_event.is_set():
                raise SynthesisCancelled("synthesis was cancelled")
            self._send({
                "request_id": request_id,
                "type": "audio",
                "session_id": session_id,
                "generation_id": generation_id,
                "speech_unit_id": speech_unit_id,
                "sequence": sequence,
                "sample_rate": sample_rate,
                "channels": channels,
                "sample_format": "s16le",
                "duration_seconds": duration_seconds,
                "data": pcm,
                "is_final": False,
            })
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
                    speed=float(options.get("speed", 1.0)),
                    seed=int(options.get("seed", -1)),
                    min_chunk_length=int(options.get("min_chunk_length", 16)),
                    streaming_mode=bool(options.get("streaming_mode", True)),
                    compute_pacing=(
                        dict(options["compute_pacing"])
                        if isinstance(options.get("compute_pacing"), dict)
                        else (
                            None
                            if options.get("compute_pacing") is False
                            else dict(DEFAULT_GPU_COMPUTE_PACING)
                        )
                    ),
                )
            self._send({
                "request_id": request_id,
                "type": "audio",
                "session_id": session_id,
                "generation_id": generation_id,
                "speech_unit_id": speech_unit_id,
                "sequence": sequence,
                "sample_rate": int(result["sample_rate"]),
                "channels": int(result["channels"]),
                "sample_format": "s16le",
                "duration_seconds": 0.0,
                "data": b"",
                "is_final": True,
            })
            self._send({"request_id": request_id, "type": "complete", "ok": True, "result": result})
        except SynthesisCancelled:
            self._send({"request_id": request_id, "type": "complete", "ok": True, "cancelled": True})
        except Exception as exc:
            self._send_error(request_id, exc)
        finally:
            with self.jobs_lock:
                self.jobs.pop(key, None)

    def _send_error(self, request_id: str, exc: Exception) -> None:
        self._send({"request_id": request_id, "type": "error", "ok": False, "error": str(exc), "traceback": traceback.format_exc()})

    def _send(self, payload: dict[str, Any]) -> None:
        with self.send_lock:
            self.connection.send(payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--auth-token", required=True)
    parser.add_argument("--app-root", required=True, type=Path)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--full-precision", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    engine = GptSovitsEngine(
        app_root=args.app_root,
        model_dir=args.model_dir,
        output_dir=args.output_dir,
        device=args.device,
        half=not args.full_precision,
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
