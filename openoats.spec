# openoats.spec - one-folder build for the installer
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = [
    "pyaudiowpatch",
    "sounddevice",
    "keyring.backends.Windows",
    "qasync",
    "silero_vad",
    # Our integrations (lazy-imported from coordinator, so list explicitly)
    "integrations.notion",
    "integrations.outlook_calendar",
    "app.recovery",
    "intelligence.llm_factory",
    "intelligence.clients.claude_cli",
    "intelligence.transcript_cleanup",
    "httpx",
    # Outlook COM (lazy-imported inside find_meeting)
    "win32com",
    "win32com.client",
    "pythoncom",
    "win32timezone",
]

for pkg in ["ctranslate2", "faster_whisper", "silero_vad"]:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OpenOats",
    console=False,
    icon=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="OpenOats",
)
