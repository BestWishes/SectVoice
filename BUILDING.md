# Building SectVoice on Windows

## Requirements

- Windows 10/11 x64
- Python 3.10 or 3.11
- Git
- FFmpeg available under the selected `SECTVOICE_ROOT`
- Inno Setup 6 and PyInstaller only when producing the installer
- NVIDIA GPU/CUDA only for the Standard package

Models and engine environments are intentionally not stored in this repository.

## Reader development environment

```powershell
git clone https://github.com/BestWishes/SectVoice.git
cd SectVoice
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
$env:SECTVOICE_ROOT = "D:\SectVoiceData"
.\.venv\Scripts\python.exe -m sectvoice
```

Keep `SECTVOICE_ROOT` outside the repository. SectVoice creates `runtime`, `models`, `data`, `cache`, `downloads`, `temp` and `artifacts` below it.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src engine_packages scripts tests
git diff --check
```

The model-independent suite may use fake engine clients at protocol boundaries. A release is not accepted until the real Basic and Standard smoke/long-run scripts also pass with real cloned audio.

## Engine/model packages

Each package contains a manifest, pinned engine source/runtime, an isolated environment and model files. The Reader discovers it through the package registry; it does not import engine internals into the GUI process.

The package manager scripts in `scripts/manage_package.py` and the manifests under `engine_packages/` are the entrypoints. Review the upstream license and update `MODEL_LICENSES.md` and `THIRD_PARTY_NOTICES.md` before changing a pin.

## Release build

The release builder expects an already prepared private staging root containing the portable runtimes and model files:

```powershell
$env:SECTVOICE_ROOT = "D:\SectVoiceBuildRoot"
.\.venv\Scripts\python.exe scripts\build_release.py --help
```

如果模型/运行时根目录与源码仓库分开，使用 `--source-root <仓库目录>`；构建器不会把模型或运行时复制回源码仓库。

Never publish a release solely because the build completed. Run the privacy audit, SHA-256 verification, clean-install/upgrade test and real engine smoke tests described in `DISTRIBUTION_PLAN.md`.

## FFmpeg and Rubber Band

The validated v0.2.x Windows distribution uses a GPLv3 Gyan FFmpeg Essentials build containing Rubber Band. That combination preserves the validated speed-control quality but imposes GPL redistribution obligations. A proprietary downstream distribution must replace it with a compatible permissive/commercial implementation or obtain a Rubber Band commercial licence. Do not relabel this build as LGPL.
