from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

from scripts.build_release import audit_no_voice_assets, sha256_file


SIDECARS = {
    "catalog.json",
    "reader-update.json",
    "README-INSTALL.txt",
    "release-manifest.json",
    "THIRD_PARTY_NOTICES.md",
    "FFMPEG_SOURCE.md",
}


def reuse_matching_assets(
    current_package: dict[str, object],
    previous_package: dict[str, object],
    *,
    previous_base_url: str | None = None,
) -> int:
    previous_by_content = {
        (int(asset["size"]), str(asset["sha256"])): asset
        for asset in previous_package["assets"]
    }
    reused = 0
    for asset in current_package["assets"]:
        previous = previous_by_content.get(
            (int(asset["size"]), str(asset["sha256"]))
        )
        if previous is None:
            continue
        previous_url = str(previous["url"])
        if not _is_remote(previous_url) and previous_base_url:
            previous_url = (
                previous_base_url.rstrip("/") + "/" + str(previous["name"])
            )
        asset.update(
            name=previous["name"],
            url=previous_url,
            size=previous["size"],
            sha256=previous["sha256"],
        )
        reused += 1
    return reused


def _is_remote(url: str) -> bool:
    return urlparse(url).scheme in {"http", "https"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reuse byte-identical immutable package assets from an older release"
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--release-root",
        type=Path,
        help="发布工作目录；默认<SectVoiceRoot>/release",
    )
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--previous-catalog", type=Path, required=True)
    parser.add_argument(
        "--previous-base-url",
        help="旧目录中相对URL对应的不可发布Release根地址",
    )
    parser.add_argument("--tier", choices=("basic", "standard"), required=True)
    args = parser.parse_args()

    root = args.root.resolve()
    release = args.release.resolve()
    release_root = (
        args.release_root.resolve()
        if args.release_root is not None
        else (root / "release").resolve()
    )
    try:
        release.relative_to(release_root)
    except ValueError as exc:
        raise RuntimeError("发布目录必须位于<SectVoiceRoot>/release内") from exc
    previous_catalog = json.loads(
        args.previous_catalog.resolve().read_text(encoding="utf-8")
    )
    previous = next(
        package
        for package in previous_catalog["packages"]
        if package["tier"] == args.tier
    )

    reused_counts: list[int] = []
    documents: list[dict[str, object]] = []
    for name in ("catalog.json", "release-manifest.json"):
        path = release / name
        document = json.loads(path.read_text(encoding="utf-8"))
        current = next(
            package for package in document["packages"] if package["tier"] == args.tier
        )
        reused_counts.append(
            reuse_matching_assets(
                current,
                previous,
                previous_base_url=args.previous_base_url,
            )
        )
        documents.append(document)
    if not reused_counts[0] or reused_counts[0] != reused_counts[1]:
        raise RuntimeError(f"复用资产数量异常：{reused_counts}")
    for name, document in zip(
        ("catalog.json", "release-manifest.json"), documents, strict=True
    ):
        (release / name).write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    manifest = documents[1]
    public_names = set(SIDECARS)
    public_names.add(str(manifest["core"]["name"]))
    public_names.add(str(manifest["installer"]["name"]))
    for package in manifest["packages"]:
        for asset in package["assets"]:
            if not _is_remote(str(asset["url"])):
                public_names.add(str(asset["name"]))

    checksum_lines: list[str] = []
    for name in sorted(public_names):
        path = release / name
        if not path.is_file():
            raise RuntimeError(f"待发布的本地资产不存在：{path}")
        checksum_lines.append(f"{sha256_file(path)}  {name}")
    (release / "SHA256SUMS.txt").write_text(
        "\n".join(checksum_lines) + "\n", encoding="utf-8"
    )
    audit_no_voice_assets(release, (root, Path.home()))
    print(f"reused {reused_counts[0]} byte-identical {args.tier} assets")
    print("public local assets:", ", ".join(sorted(public_names)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
