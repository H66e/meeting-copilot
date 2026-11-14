"""构建、启动自验并打包 Windows x64 免安装版本。

用法：python tools/build_windows.py
必须在 Windows 且已安装 requirements.txt 与 pyinstaller 的环境中执行。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "MeetingCopilot"
VERSION = (ROOT / "packaging" / "version.txt").read_text(encoding="utf-8").strip()
ZIP = ROOT / "dist" / f"MeetingCopilot-v{VERSION}-Windows-x64.zip"
CHECKSUM = ZIP.with_suffix(".zip.sha256")


def run(command: list[str], *, cwd: Path = ROOT, timeout: int = 900,
        env: dict[str, str] | None = None) -> None:
    print("+", " ".join(str(part) for part in command), flush=True)
    result = subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, env=env,
                            encoding="utf-8", errors="replace", timeout=timeout)
    if result.stdout.strip():
        print(result.stdout, flush=True)
    if result.returncode:
        raise RuntimeError(f"命令失败（{result.returncode}）：{command[0]}")


def stage_release() -> None:
    """只复制明确的公共文件，绝不收集本机配置或会议数据。"""
    for source, target in (
        ("config.example.yaml", "config.example.yaml"),
        ("docs/RELEASE_WINDOWS.md", "使用说明.txt"),
        ("LICENSE", "LICENSE.txt"),
    ):
        shutil.copy2(ROOT / source, DIST / target)
    images = DIST / "docs" / "images"
    images.mkdir(parents=True, exist_ok=True)
    for name in ("meeting-copilot.png", "meeting-copilot-settings.png"):
        shutil.copy2(ROOT / "docs" / "images" / name, images / name)
    for name in ("data", "records", "exports", "logs"):
        (DIST / name).mkdir(exist_ok=True)
    forbidden = ("config.yaml", ".env", "data/ui_state.json", "logs/assistent.log")
    leaked = [name for name in forbidden if (DIST / name).exists()]
    if leaked:
        raise RuntimeError(f"发布目录含私人文件：{leaked}")


def smoke_test() -> None:
    exe = DIST / "MeetingCopilot.exe"
    env = os.environ.copy()
    env["MC_DISABLE_CAPTURE_EXCLUDE"] = "1"
    run([str(exe), "--smoke-test"], cwd=DIST, timeout=90, env=env)
    if not exe.exists():
        raise RuntimeError("未找到打包后的 MeetingCopilot.exe")


def make_zip() -> None:
    if ZIP.exists():
        ZIP.unlink()
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(DIST.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(DIST)
            # 启动自验会生成日志与窗口状态；用户数据目录只保留在本地，不进 ZIP。
            if relative.parts[0] in {"data", "records", "exports", "logs"}:
                continue
            if relative.name == "config.yaml" or relative.name.startswith(".env"):
                continue
            archive.write(path, path.relative_to(DIST.parent))
    with zipfile.ZipFile(ZIP) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"ZIP 校验失败：{bad}")
        names = set(archive.namelist())
        forbidden = ("MeetingCopilot/config.yaml", "MeetingCopilot/data/",
                     "MeetingCopilot/logs/", "MeetingCopilot/records/",
                     "MeetingCopilot/exports/", "MeetingCopilot/.env")
        if any(any(name.startswith(prefix) for prefix in forbidden) for name in names):
            raise RuntimeError("ZIP 不得包含本机配置、日志或用户数据")
    digest = hashlib.sha256(ZIP.read_bytes()).hexdigest()
    CHECKSUM.write_text(f"{digest}  {ZIP.name}\n", encoding="ascii")


def main() -> int:
    if os.name != "nt":
        raise SystemExit("Windows 发布包只能在 Windows 上构建")
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         str(ROOT / "packaging" / "meeting_copilot.spec")])
    stage_release()
    smoke_test()
    make_zip()
    print(f"发布包：{ZIP}（{ZIP.stat().st_size / 1024**2:.1f} MB）")
    print(f"SHA256：{CHECKSUM}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
