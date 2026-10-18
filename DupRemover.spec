# Build with:  pyinstaller DupRemover.spec   (output: dist/DupRemover/DupRemover.exe)
from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ["packaging/launcher.py"],
    pathex=["src"],
    datas=[("src/dupremover/ui", "dupremover/ui")],
    hiddenimports=collect_submodules("webview"),
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="DupRemover", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="DupRemover")
