# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

source_root = Path(SPECPATH).parent
opencc_datas = collect_data_files("opencc")
icon_path = source_root / "src" / "sectvoice" / "assets" / "VoiceIcon.ico"
builtin_voice_root = source_root / "src" / "sectvoice" / "assets" / "builtin_voices"
branding_datas = [
    (str(icon_path), "sectvoice/assets"),
    (str(icon_path.with_suffix(".png")), "sectvoice/assets"),
    (str(builtin_voice_root), "sectvoice/assets/builtin_voices"),
]
reader_analysis = Analysis(
    [str(source_root / "src" / "sectvoice" / "__main__.py")],
    pathex=[str(source_root / "src")],
    binaries=[],
    datas=opencc_datas + branding_datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
    optimize=0,
)
reader_pyz = PYZ(reader_analysis.pure)
reader_exe = EXE(
    reader_pyz,
    reader_analysis.scripts,
    [],
    exclude_binaries=True,
    name="SectVoiceReader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(icon_path),
)

package_analysis = Analysis(
    [str(source_root / "src" / "sectvoice" / "package_cli.py")],
    pathex=[str(source_root / "src")],
    binaries=[],
    datas=opencc_datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
    optimize=0,
)
package_pyz = PYZ(package_analysis.pure)
package_exe = EXE(
    package_pyz,
    package_analysis.scripts,
    [],
    exclude_binaries=True,
    name="SectVoicePackage",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    icon=str(icon_path),
)

collect = COLLECT(
    reader_exe,
    package_exe,
    reader_analysis.binaries,
    reader_analysis.datas,
    package_analysis.binaries,
    package_analysis.datas,
    strip=False,
    upx=False,
    name="app",
)
