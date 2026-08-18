from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

from scripts.build_release import audit_no_voice_assets, sha256_file


PUBLIC_DOCUMENTS = (
    "README-INSTALL.txt",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
    "FFMPEG_SOURCE.md",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Finalize an immutable SectVoice release with installer and checksums"
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--source-root",
        type=Path,
        help="源码目录；默认<SectVoiceRoot>/source",
    )
    parser.add_argument(
        "--release-root",
        type=Path,
        help="发布工作目录；默认<SectVoiceRoot>/release",
    )
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--installer", type=Path, required=True)
    args = parser.parse_args()

    root = args.root.resolve()
    release = args.release.resolve()
    installer = args.installer.resolve()
    release_root = (
        args.release_root.resolve()
        if args.release_root is not None
        else (root / "release").resolve()
    )
    try:
        release.relative_to(release_root)
    except ValueError as exc:
        raise RuntimeError("最终发布目录必须位于<SectVoiceRoot>/release内") from exc
    if not release.is_dir() or not (release / "release-manifest.json").is_file():
        raise RuntimeError("发布目录缺少release-manifest.json")
    if not installer.is_file():
        raise RuntimeError(f"安装程序不存在：{installer}")

    installer_target = release / installer.name
    if installer_target.exists():
        raise RuntimeError(f"拒绝覆盖已经最终化的资产：{installer_target}")
    shutil.copy2(installer, installer_target)
    source_root = (
        args.source_root.resolve()
        if args.source_root is not None
        else root / "source"
    )
    for name in PUBLIC_DOCUMENTS:
        source = (
            source_root / "distribution" / name
            if name in {"README-INSTALL.txt", "FFMPEG_SOURCE.md"}
            else source_root / name
        )
        target = release / name
        if target.exists():
            raise RuntimeError(f"拒绝覆盖已经最终化的资产：{target}")
        shutil.copy2(source, target)

    manifest_path = release / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["installer"] = {
        "name": installer_target.name,
        "size": installer_target.stat().st_size,
        "sha256": sha256_file(installer_target),
        "signed": False,
    }
    manifest["public_documents"] = list(PUBLIC_DOCUMENTS)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    update_manifest = {
        "schema_version": 1,
        "version": str(manifest["version"]),
        "release_notes_url": (
            "https://github.com/BestWishes/SectVoice-Downloads/releases/tag/"
            f"v{manifest['version']}"
        ),
        "installer": {
            **manifest["installer"],
            "url": installer_target.name,
        },
    }
    (release / "reader-update.json").write_text(
        json.dumps(update_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    audit_no_voice_assets(release, (root, Path.home()))
    checksum_lines = []
    for path in sorted(release.iterdir()):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            checksum_lines.append(f"{sha256_file(path)}  {path.name}")
    (release / "SHA256SUMS.txt").write_text(
        "\n".join(checksum_lines) + "\n", encoding="utf-8"
    )
    print(release)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
