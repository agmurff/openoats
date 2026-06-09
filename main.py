import os
# MUST be set before any import that pulls torch / ctranslate2. PyInstaller
# bundles libiomp5md.dll from BOTH torch and ctranslate2; without this flag
# ctranslate2 SIGABRTs the moment WhisperModel loads on Record.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import sys
import asyncio
import logging
from pathlib import Path
import qasync
from PySide6.QtWidgets import QApplication
from app.coordinator import AppCoordinator
from app.settings import AppSettings
from ui.main_window import MainWindow
from ui.styles import DARK_THEME

# Log to %LOCALAPPDATA%\OpenOats\openoats.log — the install dir under Program Files
# is read-only for non-elevated users.
_log_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "OpenOats"
_log_dir.mkdir(parents=True, exist_ok=True)
_log_path = _log_dir / "openoats.log"

# Build handler list — only add a stderr StreamHandler if stderr actually exists.
# Under PyInstaller console=False, sys.stderr is None and StreamHandler.emit crashes.
_handlers: list[logging.Handler] = [logging.FileHandler(str(_log_path), encoding="utf-8")]
if sys.stderr is not None:
    _handlers.append(logging.StreamHandler(sys.stderr))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=_handlers,
)
# Capture everything that would otherwise die silently in a windowed build.
from app.diagnostics import install as _install_diagnostics, attach_to_loop as _attach_diag_loop
_install_diagnostics(_log_path)
# Keep our own modules at INFO
for _mod in ("app", "audio", "transcription", "intelligence", "storage"):
    logging.getLogger(_mod).setLevel(logging.INFO)


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_THEME)

    settings = AppSettings()

    # First-run onboarding
    if not settings.recording_consent_acknowledged:
        from ui.onboarding_dialog import OnboardingDialog
        dialog = OnboardingDialog(settings)
        if dialog.exec() != OnboardingDialog.DialogCode.Accepted:
            sys.exit(0)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    _attach_diag_loop(loop)  # qasync just installed the loop — capture its task errors

    coordinator = AppCoordinator(settings=settings)
    window = MainWindow(coordinator)
    window.show()

    # Prereq check — local LLM stack lives outside the installer
    from app.prereq_check import check_prereqs, build_message
    status = check_prereqs(
        ollama_url=settings.ollama_base_url,
        llm_model=settings.ollama_llm_model,
        embed_model=settings.ollama_embedding_model,
        mempalace_exe=settings.mempalace_exe,
    )
    if not status.all_ok:
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(window)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("OpenOats — Setup needed")
        box.setText("Some local components are missing.")
        box.setInformativeText(build_message(status, settings.ollama_llm_model,
                                             settings.ollama_embedding_model))
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.show()

    # GitHub update check — non-blocking; pops a dialog only if newer release is found
    if settings.check_for_updates:
        from app.updater import schedule_check
        from app import __version__ as _CUR

        def _on_release(info: dict) -> None:
            from PySide6.QtWidgets import QMessageBox
            import webbrowser
            box = QMessageBox(window)
            box.setIcon(QMessageBox.Icon.Information)
            box.setWindowTitle("OpenOats update available")
            box.setText(f"A newer release is available: {info['tag']} (you have v{_CUR}).")
            body = (info.get("body") or "")[:600]
            box.setInformativeText(body + "\n\nOpen the release page to download?")
            box.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if box.exec() == QMessageBox.StandardButton.Yes:
                webbrowser.open(info.get("asset_url") or info.get("url"))

        loop.call_soon(
            lambda: schedule_check(settings.github_owner, settings.github_repo, _on_release)
        )

    from ui.system_tray import SystemTray
    window._tray = SystemTray(main_window=window, coordinator=coordinator)

    async def _startup():
        if coordinator.kb:
            window._kb_label.setText("Indexing KB...")
            await coordinator.kb.index(
                progress_cb=lambda done, total: window._kb_label.setText(
                    f"Indexing KB: {done}/{total}"
                )
            )
            window._kb_label.setText(f"KB: {coordinator.kb.chunk_count} chunks")

    loop.call_soon(lambda: asyncio.ensure_future(_startup()))

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        logging.getLogger("openoats").critical("Fatal error in main()", exc_info=True)
        raise
