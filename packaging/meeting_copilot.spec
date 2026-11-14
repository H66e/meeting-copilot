# PyInstaller onedir：用户数据在 exe 旁，第三方运行库位于 _internal。
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
datas = [
    (str(ROOT / "config.example.yaml"), "."),
    (str(ROOT / "assets" / "app.ico"), "assets"),
]
binaries = []
hiddenimports = [
    "audio_capture", "asr_engine", "brain", "vision_reader", "knowledge_base",
    "hotkeys", "ui_split", "ui_overlay", "tools.selfcheck", "PIL.ImageGrab",
]

for package in ("rapidocr", "onnxruntime", "av", "ctranslate2"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

for package in ("faster_whisper", "jieba"):
    datas += collect_data_files(package)
    hiddenimports += collect_submodules(package)

analysis = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tests"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="MeetingCopilot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / "assets" / "app.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
)
collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MeetingCopilot",
)
