"""Compact dark workstation theme for QYS RTOS."""

STYLE = """
QWidget { background: #101720; color: #dce7f3; font-family: "Microsoft YaHei UI", "Segoe UI"; font-size: 10pt; }
QMainWindow, QWidget#appRoot { background: #0c121a; }
QLabel { background: transparent; }
QLabel#brandMark { background: #4d9dff; color: #071421; font-family: "Segoe UI"; font-size: 19pt; font-weight: 800; border-radius: 10px; min-width: 44px; max-width: 44px; min-height: 44px; max-height: 44px; }
QLabel#appTitle { color: #f5f8fc; font-family: "Segoe UI"; font-size: 16pt; font-weight: 700; }
QLabel#appSubtitle { color: #8497ac; font-family: "Segoe UI"; font-size: 8pt; letter-spacing: 1px; }
QLabel#environmentBadge { color: #91a4b9; background: #1a2733; border: 1px solid #2d3b4a; border-radius: 12px; padding: 5px 10px; }
QLabel#environmentBadge[ready="true"] { color: #89ddb4; background: #143128; border: 1px solid #255d48; }
QLabel#projectPath, QLabel#mutedLabel { color: #8b9eb1; font-size: 9pt; }
QLabel#shellPrompt { color: #70b8ff; font-family: Consolas; font-weight: 700; }
QFrame#controlBar, QFrame#consolePanel, QFrame#advancedBar { background: #151e29; border: 1px solid #263543; border-radius: 9px; }
QFrame#controlBar QLabel, QFrame#advancedBar QLabel { color: #a7b8c8; }
QPushButton { background: #222f3d; color: #dce8f3; border: 1px solid #34485a; border-radius: 6px; padding: 7px 13px; min-height: 19px; }
QPushButton:hover { background: #2b3c4e; border-color: #58718a; }
QPushButton:pressed { background: #192838; }
QPushButton:disabled { color: #687b8d; background: #18222d; border-color: #273542; }
QPushButton#primaryButton { background: #4d9dff; color: #071421; border-color: #4d9dff; font-weight: 700; padding-left: 19px; padding-right: 19px; }
QPushButton#primaryButton:hover { background: #80b9ff; }
QPushButton#primaryButton:disabled { background: #2a4058; color: #7e93a8; border-color: #2a4058; }
QPushButton#buildButton { background: #25445c; color: #d9efff; border-color: #356789; font-weight: 600; }
QPushButton#buildButton:disabled { color: #687b8d; background: #18222d; border-color: #273542; }
QLineEdit, QComboBox, QPlainTextEdit, QTreeView { background: #0f1721; color: #dce7f3; border: 1px solid #304050; border-radius: 5px; selection-background-color: #315d84; selection-color: #ffffff; }
QLineEdit { padding: 6px 8px; min-height: 20px; }
QComboBox { padding: 5px 8px; min-height: 23px; }
QComboBox::drop-down { border: 0; width: 22px; }
QComboBox QAbstractItemView { background: #192531; selection-background-color: #315d84; color: #edf4fb; border: 1px solid #34485a; }
QPlainTextEdit { padding: 4px; }
QPlainTextEdit#outputBox { background: #0c131c; border-color: #263543; }
QTreeView { padding: 5px; outline: 0; }
QTreeView::item { padding: 3px 2px; }
QTreeView::item:selected { background: #294d6b; border-radius: 3px; }
QTabWidget::pane { border: 1px solid #304050; background: #101720; border-radius: 5px; }
QTabBar::tab { background: transparent; color: #8ea2b6; padding: 8px 15px; border: 0; }
QTabBar::tab:selected { color: #eef6ff; border-bottom: 2px solid #65aeff; }
QTabBar::tab:hover { color: #cfe8ff; }
QTabBar#workbenchModes::tab { background: #151e29; border: 1px solid #304050; border-radius: 6px; color: #a7b8c8; margin-left: 6px; padding: 8px 16px; }
QTabBar#workbenchModes::tab:selected { background: #25445c; border-color: #548cc1; color: #eef6ff; font-weight: 700; }
QTabBar#workbenchModes::tab:!selected:hover { background: #222f3d; color: #eef6ff; }
QSplitter::handle { background: #263543; }
QStatusBar { color: #a7bacb; background: #101923; }
QStatusBar::item { border: 0; }
QDialog { background: #131d27; }
QGroupBox { border: 1px solid #34485a; border-radius: 6px; margin-top: 12px; padding: 12px 8px 8px; }
QGroupBox::title { color: #c9d8e6; subcontrol-origin: margin; left: 10px; padding: 0 5px; }
QCheckBox { spacing: 6px; }
QProgressBar { border: 1px solid #34485a; border-radius: 5px; background: #0f1721; text-align: center; }
QProgressBar::chunk { background: #4d9dff; border-radius: 4px; }
QScrollBar:vertical { background: #111a24; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #3c5266; border-radius: 5px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QLabel#catalogTitle { font-size: 23px; font-weight: 700; color: #f2f7ff; }
QLabel#catalogBoardTitle { font-size: 17px; font-weight: 650; color: #f2f7ff; }
QTreeWidget#catalogBoards { alternate-background-color: #18232f; }
QTreeWidget#catalogBoards::item { padding: 4px 7px; }
QTreeWidget#catalogBoards::item:selected { background: #265477; color: #f0f6ff; }
QHeaderView::section { background: #202d3b; color: #9db6cc; border: 0; border-bottom: 1px solid #2b4054; padding: 7px 10px; }
"""
