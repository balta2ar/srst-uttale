#!/usr/bin/env python3

import logging
from bisect import bisect_left
from dataclasses import dataclass
from json import dumps, loads
from os import environ
from pathlib import Path
from subprocess import PIPE, STDOUT, Popen
from sys import argv, exit
from tempfile import gettempdir
from time import perf_counter
from typing import List, Optional
from urllib.error import URLError
from urllib.parse import quote, urlencode
from urllib.request import urlopen

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QCursor, QFont, QKeyEvent
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

class UttaleAPI:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.logger = logging.getLogger("UttaleAPI")

    def _make_request(self, endpoint: str, params: Optional[dict] = None) -> dict:
        try:
            url = f"{self.base_url}{endpoint}"
            if params:
                url += "?" + urlencode(params)

            self.logger.info(url)
            start_time = perf_counter()

            with urlopen(url) as response:
                data = response.read()
                response_time = perf_counter() - start_time

                response_json = loads(data.decode())
                self.logger.info(f"Received in {response_time:.3f}s: {len(response_json)}")
                return response_json

        except URLError as e:
            self.logger.error(f"API Error: {e}")
            return None

    def search_scopes(self, query: str, limit: int = 1000) -> List[str]:
        result = self._make_request("/uttale/Scopes", {
            "q": query,
            "limit": limit,
        })
        if result and isinstance(result.get("results"), list):
            return result["results"]
        return []

    def search_text(self, query: str, scope: str = "", limit: int = 1000) -> List['SearchResult']:
        result = self._make_request("/uttale/Search", {
            "q": query,
            "scope": scope,
            "limit": limit,
        })
        if result and isinstance(result.get("results"), list):
            return [SearchResult(**item) for item in result["results"]]
        return []

    def get_audio_url(self, filename: str, start: str = "", end: str = "") -> str:
        return (f"{self.base_url}/uttale/Audio?"
            f"filename={filename}&"
            f"start={start}&"
            f"end={end}")

def timestamp_to_seconds(timestamp: str) -> float:
    time_parts = timestamp.split(":")
    if len(time_parts) == 3:  # HH:MM:SS.mmm
        h, m, s = time_parts
        s, ms = s.split(".")
        return int(h) * 3600 + int(m) * 60 + int(s) + float(f"0.{ms}")
    # otherwise: MM:SS.mmm
    m, s = time_parts
    s, ms = s.split(".")
    return int(m) * 60 + int(s) + float(f"0.{ms}")

@dataclass
class SearchResult:
    filename: str
    text: str
    start: str
    end: str
    def offset(self, api: UttaleAPI) -> int:
        # TODO: this is not efficient to send a new request for each offset,
        # it's server that should return the offset in search results
        results = api.search_text(query="", scope=self.filename)
        for i, result in enumerate(results):
            if result.start == self.start:
                return i
        return 0

class SearchUI(QMainWindow):
    def __init__(self):
        super().__init__()
        base_url = environ.get("UTTALE_API", "http://localhost:7010")
        self.api = UttaleAPI(base_url)

        self.setWindowTitle("Uttale")
        self.setObjectName("Uttale")

        app_font = QFont()
        app_font.setPointSize(22)
        QApplication.setFont(app_font)

        self.setup_ui()
        self.setup_timers()
        self.setup_temporary_storage()
        self.load_saved_state()
        self._last_highlighted_idx = None
        self.player_start_time = None
        self.is_player_paused = False

    def setup_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)

        self.tab_widget = QTabWidget()
        layout.addWidget(self.tab_widget)

        self.search_tab = QWidget()
        search_layout = QVBoxLayout(self.search_tab)

        self.scope_search = QLineEdit()
        self.scope_search.setPlaceholderText("Search scopes...")
        search_layout.addWidget(self.scope_search)

        self.scope_suggestions = QListWidget()
        self.scope_suggestions.setMaximumHeight(250)
        self.scope_suggestions.hide()
        search_layout.addWidget(self.scope_suggestions)

        self.text_search = QLineEdit()
        self.text_search.setPlaceholderText("Search text...")
        search_layout.addWidget(self.text_search)

        self.results_list = QListWidget()
        search_layout.addWidget(self.results_list)

        self.tab_widget.addTab(self.search_tab, "Search")

        self.episode_tab = QWidget()
        episode_layout = QVBoxLayout(self.episode_tab)

        self.episode_scope_search = QLineEdit()
        self.episode_scope_search.setPlaceholderText("Search scopes...")
        episode_layout.addWidget(self.episode_scope_search)

        self.episode_scope_suggestions = QListWidget()
        self.episode_scope_suggestions.setMaximumHeight(100)
        self.episode_scope_suggestions.hide()
        episode_layout.addWidget(self.episode_scope_suggestions)

        self.episode_start_times = []
        self.episode_results = QListWidget()
        episode_layout.addWidget(self.episode_results)

        self.tab_widget.addTab(self.episode_tab, "Episode")

        self.help_tab = QWidget()
        help_layout = QVBoxLayout(self.help_tab)

        self.help_text = QTextEdit()
        self.help_text.setReadOnly(True)
        self.help_text.setHtml("""
            ctrl-l: Focus scope search<br>
            ctrl-k: Focus text search<br>
            alt-!: Focus search tab<br>
            alt-@: Focus episode tab<br>
            alt-#: Focus help tab<br>
            ctrl-t: Pause/play audio<br>
""")
        help_layout.addWidget(self.help_text)

        self.tab_widget.addTab(self.help_tab, "Help")

        self.scope_search.textChanged.connect(self.on_scope_search_changed)
        self.text_search.textChanged.connect(self.on_text_search_changed)
        self.scope_suggestions.itemClicked.connect(self.on_scope_selected)

        self.episode_scope_search.textChanged.connect(self.on_episode_scope_search_changed)
        self.episode_scope_suggestions.itemClicked.connect(self.on_episode_scope_selected)

        for widget in [main_widget, self.scope_search, self.text_search, self.episode_scope_search]:
            widget.installEventFilter(self)

    def toggle_player_state(self):
        if not self.current_player:
            return

        if self.is_player_paused:
            self.current_player.stdin.write("cycle pause\n")
            self.player_start_time = perf_counter() - self.pause_position
            self.is_player_paused = False
        else:
            self.pause_position = perf_counter() - self.player_start_time
            self.current_player.stdin.write("cycle pause\n")
            self.is_player_paused = True
        self.current_player.stdin.flush()

    def eventFilter(self, obj, event):
        if event.type() == event.Type.KeyPress and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            if event.key() == Qt.Key.Key_K:
                self.tab_widget.setCurrentWidget(self.search_tab)
                self.text_search.setFocus()
                self.text_search.selectAll()
                return True
            if event.key() == Qt.Key.Key_L:
                self.tab_widget.setCurrentWidget(self.search_tab)
                self.scope_search.setFocus()
                self.scope_search.selectAll()
                return True
            if event.key() == Qt.Key.Key_T and not isinstance(obj, QLineEdit):
                self.toggle_player_state()
                return True
        elif event.type() == event.Type.KeyPress and event.modifiers() == Qt.KeyboardModifier.AltModifier:
            if event.key() == Qt.Key.Key_Exclam:
                self.tab_widget.setCurrentIndex(0)
                return True
            if event.key() == Qt.Key.Key_At:
                self.tab_widget.setCurrentIndex(1)
                return True
            if event.key() == Qt.Key.Key_NumberSign:
                self.tab_widget.setCurrentIndex(2)
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape or (event.modifiers() == Qt.KeyboardModifier.ControlModifier and event.key() == Qt.Key.Key_Q):
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

        self.episode_scope_timer = QTimer()
        self.episode_scope_timer.setSingleShot(True)
        self.episode_scope_timer.timeout.connect(self.search_episode_scopes)

        self.current_player = None
        self.current_episode_url = None
        self.player_monitor_timer = QTimer()
        self.player_monitor_timer.setInterval(100)
        self.player_monitor_timer.timeout.connect(self.monitor_player_position)

    def setup_temporary_storage(self):
        self.temp_dir = Path(gettempdir()) / "uttale_audio"
        self.temp_dir.mkdir(exist_ok=True)
        self.state_file = self.temp_dir / "search_state.json"

    def save_state(self):
        screen = QApplication.screenAt(QCursor.pos())
        state = {
            "scope": self.scope_search.text(),
            "text": self.text_search.text(),
            "episode_scope": self.episode_scope_search.text(),
            "current_tab": self.tab_widget.currentIndex(),
            "geometry": {
                "x": self.x(),
                "y": self.y(),
                "width": self.width(),
                "height": self.height(),
            },
            "screen": screen.name() if screen else None,
        }
        self.state_file.write_text(dumps(state))

    def load_saved_state(self):
        if self.state_file.exists():
            try:
                state = loads(self.state_file.read_text())
                self.scope_search.setText(state.get("scope", ""))
                self.text_search.setText(state.get("text", ""))
                self.episode_scope_search.setText(state.get("episode_scope", ""))
                self.tab_widget.setCurrentIndex(state.get("current_tab", 0))

                saved_screen = state.get("screen")
                if saved_screen:
                    for screen in QApplication.screens():
                        if screen.name() == saved_screen:
                            geo = self.frameGeometry()
                            geo.moveCenter(screen.geometry().center())
                            self.move(geo.topLeft())
                            break

                geometry = state.get("geometry", {})
                if geometry:
                    self.setGeometry(
                        geometry.get("x", 100),
                        geometry.get("y", 100),
                        geometry.get("width", 800),
                        geometry.get("height", 600),
                    )
            except:
                pass

    def on_scope_search_changed(self):
        self.scope_timer.start(200)
        self.search_timer.start(500)
        self.save_timer.start(1000)

    def on_text_search_changed(self):
        self.search_timer.start(500)
        self.save_timer.start(1000)

    def on_episode_scope_search_changed(self):
        self.episode_scope_timer.start(200)
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

    def search_episode_scopes(self):
        query = self.episode_scope_search.text()
        scopes = self.api.search_scopes(query)

        self.episode_scope_suggestions.clear()
        if scopes:
            self.episode_scope_suggestions.show()
            self.episode_scope_suggestions.addItems(scopes)
        else:
            self.episode_scope_suggestions.hide()

    def on_scope_selected(self, item):
        self.scope_search.setText(item.text())
        self.scope_suggestions.hide()
        self.search_text()

    def on_episode_scope_selected(self, item: QListWidgetItem, index: int = -1) -> None:
        if not item:
            return

        scope = item.text()
        self.current_episode_url = self.api.get_audio_url(scope)
        results = self.api.search_text("", scope)

        self.episode_results.clear()
        self.episode_start_times = []

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
                lambda checked, r=result: self.play_episode_from(r))

            item_layout.addWidget(play_button)
            item_layout.addWidget(text_button)

            self.episode_start_times.append(timestamp_to_seconds(result.start))
            self.episode_results.addItem("")
            item = self.episode_results.item(self.episode_results.count()-1)
            self.episode_results.setItemWidget(item, item_widget)

            if len(self.episode_start_times) - 1 == index:
                self.episode_results.scrollToItem(item)
                text_button.setStyleSheet("text-align: left; background-color: yellow;")

    def monitor_player_position(self):
        if not self.current_player or not self.player_start_time:
            self.player_monitor_timer.stop()
            return

        try:
            if not self.is_player_paused:
                position = perf_counter() - self.player_start_time
                self.highlight_current_position(position)

        except Exception as e:
            logging.exception(f"Error monitoring player: {e}")
            self.stop_episode_playback()

    def highlight_current_position(self, position: float) -> None:
        idx = bisect_left(self.episode_start_times, position) - 1
        idx = max(0, min(len(self.episode_start_times)-1, idx))

        if self._last_highlighted_idx is not None:
            last_item = self.episode_results.item(self._last_highlighted_idx)
            last_widget = self.episode_results.itemWidget(last_item)
            last_widget.layout().itemAt(1).widget().setStyleSheet("text-align: left;")

        item = self.episode_results.item(idx)
        widget = self.episode_results.itemWidget(item)
        widget.layout().itemAt(1).widget().setStyleSheet("text-align: left; background-color: lightgreen;")
        self._last_highlighted_idx = idx

    def play_episode_from(self, result: SearchResult):
        if not self.current_episode_url:
            return

        self.stop_episode_playback()

        try:
            start_time = timestamp_to_seconds(result.start)

            url = self.api.get_audio_url(result.filename)
            cmd = ["mpv", "--no-terminal", "--af=loudnorm", "--af=dynaudnorm", "--input-ipc-server=/tmp/mpvsocket", url]
            logging.info("cmd: %s", cmd)
            self.current_player = Popen(
                cmd,
                stdin=PIPE,
                stdout=None,
                stderr=STDOUT,
                text=True,
                bufsize=1,
            )

            self.player_start_time = perf_counter() - start_time
            self.is_player_paused = False
            self.player_monitor_timer.start()

        except Exception as e:
            logging.exception(f"Error playing episode: {e}")

    def stop_episode_playback(self):
        if self.current_player:
            self.current_player.terminate()
            self.current_player = None
            self.player_start_time = None
            self.player_monitor_timer.stop()

            for i in range(self.episode_results.count()):
                item = self.episode_results.item(i)
                item_widget = self.episode_results.itemWidget(item)
                text_button = item_widget.layout().itemAt(1).widget()
                text_button.setStyleSheet("text-align: left;")

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
            text_button.clicked.connect(
                lambda checked, r=result: self.show_episode(r))

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

    def show_episode(self, result: SearchResult):
        self.tab_widget.setCurrentIndex(1)
        self.episode_scope_search.setText(result.filename)

        item = QListWidgetItem(result.filename)
        self.episode_scope_suggestions.clear()
        self.episode_scope_suggestions.addItem(item)
        self.on_episode_scope_selected(item, result.offset(self.api))

    def play_audio(self, result: SearchResult):
        if self.current_player:
            self.current_player.terminate()
            self.current_player = None
            self.player_start_time = None

        try:
            url = self.api.get_audio_url(result.filename, result.start, result.end)
            cmd = ["mpv", "--no-terminal", "--af=loudnorm", "--af=dynaudnorm", "--input-ipc-server=/tmp/mpvsocket", url]
            logging.info("cmd: %s", cmd)
            self.current_player = Popen(
                cmd,
                stdin=PIPE,
                stdout=None,
                stderr=STDOUT,
                text=True,
                bufsize=1
            )

        except Exception as e:
            logging.exception(f"Error playing audio: {e}")

    def closeEvent(self, event):
        if self.current_player:
            self.current_player.terminate()
            self.current_player = None
            self.player_start_time = None

        self.save_state()
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
