"""Application entry point for QYS RTOS."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from .style import STYLE
from .window import MainWindow


def app_icon() -> QIcon:
    assets_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    icon_path = assets_root / "assets" / "qys_v1_1.ico"
    if icon_path.is_file():
        icon = QIcon(str(icon_path))
        if not icon.isNull():
            return icon
    pixmap = QPixmap(128, 128)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#4d9dff"))
    painter.drawRoundedRect(7, 7, 114, 114, 28, 28)
    painter.setPen(QColor("#071421"))
    font = QFont("Segoe UI", 68)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "Q")
    painter.end()
    return QIcon(pixmap)


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv)
    report_path = None
    if len(arguments) == 3 and arguments[1] == "--qys-self-check":
        report_path = Path(arguments[2]).expanduser().resolve()
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ["QYS_CONFIG_HOME"] = str(report_path.parent / (report_path.stem + "-settings"))
    app = QApplication([arguments[0]])
    app.setApplicationName("QYS RTOS")
    app.setOrganizationName("QYS")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setWindowIcon(app_icon())
    app.setStyleSheet(STYLE)
    window = MainWindow()
    window.show()
    if report_path is not None:
        completed = {"value": False}
        build_result: dict[str, object] = {}
        verification_build_dir = os.environ.get("QYS_SELF_CHECK_BUILD_DIR", "").strip()

        def finish_check() -> None:
            if completed["value"]:
                return
            completed["value"] = True
            import serial
            import yaml
            from PySide6 import __version__ as qt_version

            report = {
                "title": window.windowTitle(),
                "qt_version": qt_version,
                "serial_version": serial.VERSION,
                "yaml_version": yaml.__version__,
                "workspace": str(window.environment.workspace) if window.environment else None,
                "project": str(window.editor.project_root) if window.editor.project_root else None,
                "board_count": window.board_combo.count(),
            }
            report.update(build_result)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            window.close()
            QTimer.singleShot(400, app.quit)

        def after_boards(_code: int, _cancelled: bool) -> None:
            if verification_build_dir and window.editor.project_root:
                window._auto_build_dir = False
                window.build_dir.setText(verification_build_dir)

                def build_done(code: int, cancelled: bool) -> None:
                    build_result.update({"build_exit": code, "build_cancelled": cancelled})
                    QTimer.singleShot(100, finish_check)

                window.operation.finished.connect(build_done)
                window.build_project()
                if not window.operation.is_running:
                    build_result["build_error"] = "The build command did not start."
                    QTimer.singleShot(100, finish_check)
            else:
                QTimer.singleShot(100, finish_check)

        def timeout_check() -> None:
            if window.operation.is_running:
                build_result["build_error"] = "Build self-check timed out."
                window.operation.finished.connect(lambda _code, _cancelled: QTimer.singleShot(100, finish_check))
                window.operation.stop()
            else:
                finish_check()

        if window.environment:
            window.board_process.finished.connect(after_boards)
            QTimer.singleShot(240000 if verification_build_dir else 20000, timeout_check)
        else:
            QTimer.singleShot(300, finish_check)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
