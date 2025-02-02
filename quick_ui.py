#!/usr/bin/env python3

from dataclasses import dataclass
from json import dumps, loads
from pathlib import Path
from subprocess import DEVNULL, Popen
from sys import argv, exit
from tempfile import gettempdir
from typing import List, Optional
from urllib.error import URLError
from urllib.parse import quote, urlencode
from urllib.request import urlopen

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QCursor, QKeyEvent
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


@dataclass
class SearchResult:
    filename: str
    text: str
    start: str
    end: str

class UttaleAPI:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _make_request(self, endpoint: str, params: Optional[dict] = None) -> dict:
        try:
            url = f"{self.base_url}{endpoint}"
            if params:
                url += "?" + urlencode(params)

            with urlopen(url) as response:
                return loads(response.read().decode())
        except URLError as e:
            print(f"API Error: {e}")
            return None

    def search_scopes(self, query: str, limit: int = 100) -> List[str]:
        result = self._make_request("/uttale/Scopes", {
            "q": query,
            "limit": limit,
        })
        return result if result else []

    def search_text(self, query: str, scope: str = "", limit: int = 100) -> List[SearchResult]:
        result = self._make_request("/uttale/Search", {
            "q": query,
            "scope": scope,
            "limit": limit,
        })
        if not result:
            return []
        return [SearchResult(**item) for item in result]

    def get_audio(self, filename: str, start: str, end: str) -> bytes:
        url = (f"{self.base_url}/uttale/Audio?"
              f"filename={quote(filename)}&"
              f"start={quote(start)}&"
              f"end={quote(end)}")

        try:
            with urlopen(url) as response:
                return response.read()
        except URLError as e:
            print(f"Audio fetch error: {e}")
            return None

class SearchUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.api = UttaleAPI("http://localhost:7010")
        self.setup_ui()
        self.setup_timers()
        self.setup_temporary_storage()
        self.load_saved_state()

    def setup_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)

        # Search bars
        self.scope_search = QLineEdit()
        self.scope_search.setPlaceholderText("Search scopes...")
        layout.addWidget(self.scope_search)

        self.scope_suggestions = QListWidget()
        self.scope_suggestions.setMaximumHeight(100)
        self.scope_suggestions.hide()
        layout.addWidget(self.scope_suggestions)

        self.text_search = QLineEdit()
        self.text_search.setPlaceholderText("Search text...")
        layout.addWidget(self.text_search)

        self.results_list = QListWidget()
        layout.addWidget(self.results_list)

        # Connect signals
        self.scope_search.textChanged.connect(self.on_scope_search_changed)
        self.text_search.textChanged.connect(self.on_text_search_changed)
        self.scope_suggestions.itemClicked.connect(self.on_scope_selected)

        # Add event filters for keyboard shortcuts
        self.scope_search.installEventFilter(self)
        self.text_search.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == event.Type.KeyPress:
            if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
                if event.key() == Qt.Key.Key_K:
                    self.text_search.setFocus()
                    self.text_search.selectAll()
                    return True
                elif event.key() == Qt.Key.Key_M:
                    self.scope_search.setFocus()
                    self.scope_search.selectAll()
                    return True
        return super().eventFilter(obj, event)
    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        elif event.modifiers() == Qt.KeyboardModifier.ControlModifier and event.key() == Qt.Key.Key_Q:
            self.close()
        else:
            super().keyPressEvent(event)

    def setup_timers(self):
        self.scope_timer = QTimer()
        self.scope_timer.setSingleShot(True)
        self.scope_timer.timeout.connect(self.search_scopes)

        self.search_timer = QTimer()
        self.search_timer.setSingleShot(True)
        self.search_timer.timeout.connect(self.search_text)

        self.save_timer = QTimer()
        self.save_timer.setSingleShot(True)
        self.save_timer.timeout.connect(self.save_state)

        self.current_player = None

    def setup_temporary_storage(self):
        self.temp_dir = Path(gettempdir()) / "uttale_audio"
        self.temp_dir.mkdir(exist_ok=True)
        self.state_file = self.temp_dir / "search_state.json"

    def save_state(self):
        screen = QApplication.screenAt(QCursor.pos())
        state = {
            'scope': self.scope_search.text(),
            'text': self.text_search.text(),
            'geometry': {
                'x': self.x(),
                'y': self.y(),
                'width': self.width(),
                'height': self.height(),
            },
            "screen": screen.name() if screen else None,
        }
        self.state_file.write_text(dumps(state))

    def load_saved_state(self):
        if self.state_file.exists():
            try:
                state = loads(self.state_file.read_text())
                self.scope_search.setText(state.get('scope', ''))
                self.text_search.setText(state.get('text', ''))

                saved_screen = state.get('screen')
                if saved_screen:
                    for screen in QApplication.screens():
                        if screen.name() == saved_screen:
                            geo = self.frameGeometry()
                            geo.moveCenter(screen.geometry().center())
                            self.move(geo.topLeft())
                            break

                geometry = state.get('geometry', {})
                if geometry:
                    self.setGeometry(
                        geometry.get('x', 100),
                        geometry.get('y', 100),
                        geometry.get('width', 800),
                        geometry.get('height', 600)
                    )
            except:
                pass

    def on_scope_search_changed(self):
        self.scope_timer.start(200)
        self.save_timer.start(1000)

    def on_text_search_changed(self):
        self.search_timer.start(1000)
        self.save_timer.start(1000)

    def search_scopes(self):
        query = self.scope_search.text()
        scopes = self.api.search_scopes(query)

        self.scope_suggestions.clear()
        if scopes:
            self.scope_suggestions.show()
            self.scope_suggestions.addItems(scopes)
        else:
            self.scope_suggestions.hide()

    def on_scope_selected(self, item):
        self.scope_search.setText(item.text())
        self.scope_suggestions.hide()
        self.search_text()

    def search_text(self):
        query = self.text_search.text()
        scope = self.scope_search.text()

        if not query:
            return

        results = self.api.search_text(query, scope)

        self.results_list.clear()
        for result in results:
            item_widget = QWidget()
            item_layout = QHBoxLayout(item_widget)
            item_layout.setContentsMargins(0, 0, 0, 0)

            text = (f"{result.text} \n"
                   f"[{result.start} - {result.end}]")
            text_button = QPushButton(text)
            text_button.setStyleSheet("text-align: left;")

            play_button = QPushButton("▶")
            play_button.setFixedWidth(30)
            play_button.clicked.connect(
                lambda checked, r=result: self.play_audio(r))

            item_layout.addWidget(play_button)
            item_layout.addWidget(text_button)

            self.results_list.addItem("")
            self.results_list.setItemWidget(
                self.results_list.item(self.results_list.count()-1),
                item_widget)

    def play_audio(self, result: SearchResult):
        if self.current_player:
            try:
                self.current_player.terminate()
            except:
                pass

        try:
            temp_file = self.temp_dir / f"audio_{hash(result.filename + result.start + result.end)}.wav"

            if not temp_file.exists():
                audio_data = self.api.get_audio(
                    result.filename, result.start, result.end)
                if audio_data:
                    temp_file.write_bytes(audio_data)
                else:
                    return

            self.current_player = Popen(
                ["mplayer", str(temp_file)],
                stdout=DEVNULL,
                stderr=DEVNULL,
            )

        except Exception as e:
            print(f"Error playing audio: {e}")

    def closeEvent(self, event):
        if self.current_player:
            self.current_player.terminate()

        # Save state before closing
        self.save_state()

        # Cleanup temp files
        for file in self.temp_dir.glob("audio_*.wav"):
            try:
                file.unlink()
            except:
                pass
        try:
            self.temp_dir.rmdir()
        except:
            pass

        super().closeEvent(event)

def main():
    app = QApplication(argv)
    window = SearchUI()
    window.show()
    exit(app.exec())

if __name__ == "__main__":
    main()
