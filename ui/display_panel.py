"""
display_panel — the Qt shell around core.display.

All rendering lives in core/display.py, which is Qt-free and unit-tested. This
file is only the widget: it asks core.display for HTML and shows it.

Kept deliberately small and defensive. Every entry point is wrapped so that a
failure here can never take the HUD down with it - the worst case is an empty
panel, not a crashed assistant.
"""

from __future__ import annotations

try:
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import (
        QWidget, QVBoxLayout, QHBoxLayout, QTextBrowser, QPushButton, QLabel,
    )
    _QT = True
except ImportError:                                     # pragma: no cover
    _QT = False
    QWidget = object                                    # type: ignore[misc,assignment]  # import-safe placeholder


try:
    from core import display as _display
except ImportError:                                     # pragma: no cover
    import display as _display                          # type: ignore


REFRESH_MS = 400        # cheap: core.display.render is string work


class DisplayPanel(QWidget):
    """The screen inside JARVIS. Anything can push to it via core.display."""

    def __init__(self, parent=None):
        if not _QT:                                     # pragma: no cover
            raise RuntimeError("PyQt6 is required for DisplayPanel")
        super().__init__(parent)
        self.setWindowTitle("JARVIS — Display")
        self.setMinimumSize(560, 420)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # header
        bar = QHBoxLayout()
        bar.setContentsMargins(10, 8, 10, 8)
        title = QLabel("DISPLAY")
        title.setStyleSheet("color:#8fa3bf; font-weight:600; letter-spacing:2px;")
        self._count = QLabel("")
        self._count.setStyleSheet("color:#4d5b70;")
        clear_btn = QPushButton("Clear")
        clear_btn.setFixedWidth(64)
        clear_btn.clicked.connect(self._on_clear)
        bar.addWidget(title)
        bar.addStretch(1)
        bar.addWidget(self._count)
        bar.addWidget(clear_btn)
        root.addLayout(bar)

        # body
        self._view = QTextBrowser(self)
        self._view.setOpenExternalLinks(True)
        self._view.setStyleSheet(
            "QTextBrowser{background:#07090f;border:0;color:#dde3ed;}"
            "QScrollBar:vertical{background:#0b0f18;width:10px;}"
        )
        root.addWidget(self._view, 1)

        # content arrives from tool threads; poll rather than reach across them
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

        self.refresh()

    # ── public ────────────────────────────────────────────────────────────────

    def refresh(self) -> None:
        try:
            items = _display.recent()
            self._view.setHtml(_display.to_html(items))
            n = len(items)
            self._count.setText(f"{n} item" + ("" if n == 1 else "s"))
        except Exception as e:
            # never let a render failure kill the panel or the HUD
            try:
                self._view.setHtml(
                    f"<pre style='color:#f87171'>Display error: {e}</pre>")
            except Exception:
                pass

    def show_panel(self) -> None:
        try:
            self.refresh()
            self.show()
            self.raise_()
            self.activateWindow()
        except Exception:
            pass

    def push(self, kind: str, payload, title: str = "") -> None:
        """Convenience for callers that already have the widget in hand."""
        try:
            _display.display(kind, payload, title=title)
            self.refresh()
        except Exception:
            pass

    # ── internal ──────────────────────────────────────────────────────────────

    def _on_clear(self) -> None:
        try:
            _display.clear()
            self.refresh()
        except Exception:
            pass
