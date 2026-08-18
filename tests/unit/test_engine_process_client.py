from pathlib import Path
from unittest.mock import Mock

from sectvoice.engines.process_client import EngineProcessClient, EngineProcessSpec


class _LauncherProcess:
    pid = 101

    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout):
        assert timeout == 10
        self.returncode = 0


def test_stop_performs_final_process_tree_cleanup_after_launcher_exits(
    tmp_path: Path,
) -> None:
    client = EngineProcessClient(
        EngineProcessSpec(
            python_executable=tmp_path / "python.exe",
            worker_script=tmp_path / "worker.py",
            working_directory=tmp_path,
            environment={},
        )
    )
    launcher = _LauncherProcess()
    client._process = launcher
    client._worker_pid = 202
    client._kill_process_tree = Mock()

    client.stop()

    assert launcher.terminated
    client._kill_process_tree.assert_called_once_with()
    assert client._process is None
    assert client._worker_pid is None
