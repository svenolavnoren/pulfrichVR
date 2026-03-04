# ------------------------------
#  pulfrichVR – main application
# ------------------------------

APP_NAME = "pulfrichVR"
APP_VERSION = "0.9-091-a"
#last change: --0 Improved save selections and tidying duplications related to fps etc
#             --a restoring Monodir that was lost 087 -> 08 in header to repair Archive all

# --- Standardbibliotek ---
import os
import sys
import platform
import math
import stat
import glob
import shlex
import subprocess
import json
from datetime import date
from pathlib import Path
import re

# --- PySide6 / Qt ---
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QLabel, QFileDialog, QVBoxLayout, QWidget,
    QGraphicsView, QGraphicsScene, QGraphicsLineItem, QGraphicsItem, QDockWidget,
    QListWidget, QListWidgetItem, QPushButton, QHBoxLayout, QAbstractItemView,
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QSpinBox, QMenu, QFrame,
    QMessageBox, QInputDialog, QPlainTextEdit, QListView, QTreeView
)
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtCore import Qt, QUrl, QTimer, QPointF, QRectF, QStandardPaths
from PySide6.QtGui import (
    QPainter, QPen, QColor, QShortcut, QCursor, QAction,
    QFont, QMouseEvent, QKeySequence
)

# ------------------------------
#  Plattform / runtime-detektion
# ------------------------------

# Rå Windows-flagga (inklusive Wine)
RAW_IS_WIN = platform.system().lower().startswith("win")

# Heuristik: körs som Windows-program via Wine?
IN_WINE = RAW_IS_WIN and (
    "WINELOADERNOEXEC" in os.environ or "WINEPREFIX" in os.environ
)

# Äkta Windows-burk (inte Wine)
IS_NATIVE_WIN = RAW_IS_WIN and not IN_WINE

# För all logik som behöver "Windows-beteende" (paths, .exe-namn osv)
# I praktiken: "vi beter oss som Windows" = äkta Windows + Wine
IS_WIN = RAW_IS_WIN

# Packad med PyInstaller eller ej
FROZEN = getattr(sys, "frozen", False)

# ------------------------------
# Plattform / runtime-detektion
# ------------------------------

# Enkel och ärlig plattforms-flagga: är vi på Windows?
IS_WIN = platform.system().lower().startswith("win")

# ------------------------------
# Paths för runtime
# ------------------------------

# Där pulfrichVR.py ligger.
# All IO (bat-filer, 360TB-bilder osv) hänger på den.
RUNTIME_DIR = Path(__file__).resolve().parent


def runtime_path(*parts: str) -> Path:
    """Sökväg relativt runtime-katalogen (bat-filer, osv)."""
    return RUNTIME_DIR.joinpath(*parts)


WORKSHOP_LABEL = "360 bat runner"  # 0.9-049
APP_TITLE = APP_NAME               # 0.9-087: use pulfrichVR as window title

# 0.9-043: arbetsmapp för one-shot-bilder (L/R-png) 
MONO_DIR_NAME = "VideoOneshot" # 0.9-091a line restored


# ------------------------------
# FFmpeg / FFprobe-kommandon
# ------------------------------


def get_ffmpeg_cmd() -> str:
    """
    Hitta ffmpeg:
    1) RUNTIME_DIR/ffmpeg(.exe)
    2) FFMPEG_CMD env eller 'ffmpeg' i PATH
    """
    exe_name = "ffmpeg.exe" if IS_WIN else "ffmpeg"

    # Först: lokalt ffmpeg bredvid pulfrichVR.py
    local = runtime_path(exe_name)
    if local.exists():
        return str(local)

    # Sedan: ev. env-override eller bara 'ffmpeg' i PATH
    return os.environ.get("FFMPEG_CMD", exe_name)


def get_ffprobe_cmd() -> str:
    """
    Hitta ffprobe:
    1) RUNTIME_DIR/ffprobe(.exe)
    2) FFPROBE_CMD env eller 'ffprobe' i PATH
    """
    exe_name = "ffprobe.exe" if IS_WIN else "ffprobe"

    local = runtime_path(exe_name)
    if local.exists():
        return str(local)

    return os.environ.get("FFPROBE_CMD", exe_name)


# Export-läge: "timestamp_center" (mitt-i-rutan med -ss)
# eller "select_by_index" (ffmpeg select=eq(n,N))
EXPORT_FRAME_PICK_MODE = "select_by_index"  # istället för "timestamp_center"


def debug_print_environment():
    """Liten hjälp-funktion när vi felsöker på olika burkar."""
    print(f"IS_WIN={IS_WIN}")
    print(f"RUNTIME_DIR={RUNTIME_DIR}")
    print(f"PYTHON_EXE={sys.executable}")
    print(f"FFMPEG={get_ffmpeg_cmd()}")
    print(f"FFPROBE={get_ffprobe_cmd()}")

#0.9-090 class replaced, total cleanup by chatgpt 5.1
class VideoOverlay(QGraphicsItem):
    """Draws dynamic guide lines (red, green, gray, white) over the video."""

    def __init__(self, video_item):
        super().__init__()
        self.video_item = video_item

        # En enda källa för "var är kompassen?"
        self._line_x = None  # type: float | None

        # En enda flagga för visning
        self._visible = True

        # Se till att overlay ritas ovanpå videon
        self.setZValue(10)

    # --- API som övrig kod använder ---

    @property
    def line_x(self):
        """Senast valda X-position (eller None om ingen)."""
        return self._line_x

    def set_line_x(self, x: float):
        """Set vertical reference position and refresh overlay."""
        self._line_x = x
        self.update()

    def set_overlay_visible(self, visible: bool):
        """Toggle visibility of the compass lines."""
        self._visible = bool(visible)
        self.update()

    # --- QGraphicsItem-gränssnitt ---

    def boundingRect(self):
        """Match the overlay area to the video item’s size."""
        if not self.video_item:
            return QRectF()
        rect = self.video_item.boundingRect()
        return QRectF(0, 0, rect.width(), rect.height())

    # --- Små hjälpare för att slippa upprepa oss ---

    @staticmethod
    def _wrap_x(x: float, width: float) -> float:
        """Håll x inom [0, width) med mod-wrap."""
        if width <= 0:
            return x
        return x % width

    @staticmethod
    def _draw_vertical(painter: QPainter, x: float, top_y: float, bottom_y: float) -> None:
        painter.drawLine(x, top_y, x, bottom_y)

    def paint(self, painter: QPainter, option, widget=None):
        # Ingen linje vald eller overlay avstängd → rita inget
        if self._line_x is None or not self._visible:
            return

        rect = self.boundingRect()
        width = rect.width()
        height = rect.height()

        # Liten luftmarginal upptill/nertill (samma som tidigare)
        top_y = 2
        bottom_y = max(0, height - 2)

        # Center line (vit, referens)
        center_x = self._wrap_x(self._line_x, width)
        painter.setPen(QPen(Qt.white, 2))
        self._draw_vertical(painter, center_x, top_y, bottom_y)

        # 90° LEFT (röd)
        left_90 = self._wrap_x(center_x - width / 4, width)
        painter.setPen(QPen(Qt.red, 2))
        self._draw_vertical(painter, left_90, top_y, bottom_y)

        # 90° RIGHT (grön, lite tunnare)
        right_90 = self._wrap_x(center_x + width / 4, width)
        painter.setPen(QPen(Qt.green, 1))
        self._draw_vertical(painter, right_90, top_y, bottom_y)

        # 180° OPPOSITE (grå)
        opposite = self._wrap_x(center_x + width / 2, width)
        painter.setPen(QPen(Qt.gray, 1))
        self._draw_vertical(painter, opposite, top_y, bottom_y)

#0.9-090 class replaced, total cleanup by chatgpt 5.1
class GraphicsVideoView(QGraphicsView):
    """Video surface with overlay line capability."""

    def __init__(self):
        super().__init__()

        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)

        self.setFocusPolicy(Qt.StrongFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setInteractive(False)
        self.setFrameShape(QFrame.NoFrame)  # 0.9-028

        # Video-item först
        self.video_item = QGraphicsVideoItem()
        self.scene.addItem(self.video_item)

        # Overlay ovanpå
        self.overlay = VideoOverlay(self.video_item)
        self.scene.addItem(self.overlay)

        # Kom ihåg senaste bredden för att kunna skala line_x vid resize
        self._last_video_width = None  # type: float | None

    # --------------------------------------------------
    # Resize: håll videon fylld & kompassen på rätt ställe
    # --------------------------------------------------
    def resizeEvent(self, event):
        """Keep video item filling the view, and keep direction line in same relative position."""
        super().resizeEvent(event)

        if not self.video_item:
            return

        # Gamla bredden innan vi ändrar storlek
        old_w = self._last_video_width

        # Sätt videons storlek till view-storleken
        self.video_item.setSize(self.size())

        # Ny bredd efter resize
        new_rect = self.video_item.boundingRect()
        new_w = new_rect.width()

        # Scen = video-rektangel
        self.scene.setSceneRect(new_rect)

        # Skala om line_x så att den behåller sin relativa position i videon
        line_x = self.overlay.line_x
        try:
            if old_w and old_w > 0 and new_w > 0 and line_x is not None:
                ratio = float(line_x) / float(old_w)
                new_x = max(0.0, min(new_w, ratio * new_w))
                self.overlay.set_line_x(new_x)
        except Exception as e:
            print(f"[RESIZE] line_x rescale failed: {e}")

        # Spara nya bredden till nästa resize
        self._last_video_width = new_w

    # --------------------------------------------------
    # Mus: vänster = sätt riktning, höger = toggla kompass
    # --------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent):
        # Höger musknapp: toggla kompasslinjerna
        if event.button() == Qt.RightButton:
            current_visible = getattr(self.overlay, "_visible", True)
            self.overlay.set_overlay_visible(not current_visible)
            return

        # Bara vänsterklick ska sätta riktning
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)

        # Mappa klicket till scen-koordinater
        scene_pt = self.mapToScene(event.position().toPoint())
        video_rect = self.video_item.boundingRect()

        # Om klicket inte träffar själva videon → ändra inte riktning (0.9-031)
        if not video_rect.contains(scene_pt):
            return super().mousePressEvent(event)

        # --- Vänsterklick: sätt vertikal linje baserat på scen-x ---
        drawn_w = video_rect.width()
        x = scene_pt.x()
        if drawn_w > 0:
            x = max(0.0, min(drawn_w, x))

        self.overlay.set_line_x(x)
        print(f"Line X set to {x:.1f}")

        super().mousePressEvent(event)

class VideoApp(QMainWindow):
    # 0.9-067
    def _make_window_title(self, clip: str | None) -> str:
        """
        Bygg fönstertitlarnas text.

        - På Windows: APP_TITLE – fullständigt filnamn
        - På andra system (Mint/Wine/mac): endast de sista 7 tecknen, t.ex. '042.mp4'
        """
        if IS_WIN:
            # Riktigt Windows: samma som tidigare
            if clip:
                return f"{APP_TITLE} – {clip}"
            else:
                return f"{APP_TITLE} – no clip loaded"
        else:
            # Linux/Mint/Wine: bara en lugn liten svans, t.ex. '042.mp4'
            if clip:
                tail = clip[-7:] if len(clip) > 7 else clip
                return tail
            else:
                # Ingen video än – behåll appnamnet så det inte blir tomt
                return APP_TITLE

    def __init__(self, video_path=None):
        super().__init__()


        # Läs in egna inställningar (senast använda mapp, cmd-läge) #0.9-013 1c)
        # 0.9-087: default = visa insv-varning
        self._show_insv_warning_flag = True

        self._settings_last_dir = None
        self._settings_cmd_mode = "c"
        self._load_settings()

        if not hasattr(self, "_used_counter"): # 0.9-084 extra check
            self._used_counter = 0


        # 0.9-055: gemensam knappstil (form) för huvudknapparna
        self._button_base_style = """
            QPushButton {
                padding: 4px 10px;
                border-radius: 6px;
            }
        """

        # 0.9-048-1 When True, update_label() leaves info_label alone (proxy work etc.)
        self._busy_label = False

        # 0.9-039  Håller koll på om en source-kö redan körs
        self._source_queue_busy = False
        self._source_queue_current = None  # senaste source-bat som startades


        # 0.9-057: lågmäld standardtitel tills vi vet mer
        self.setWindowTitle(APP_TITLE)

        # 0.9-053: starta i maximerat läge (bäst för både 16:9 och 16:10)
        self.setWindowState(self.windowState() | Qt.WindowMaximized)

        # --- UI Setup ---
        self.video_view = GraphicsVideoView()

        # Layout setup
        layout = QVBoxLayout()
        layout.setContentsMargins(6, 6, 6, 6) #0.9-028
        layout.setSpacing(0)
        layout.addWidget(self.video_view)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)
        
        # --- BAT Runner dock (internal) / 3D workshop (user-facing) ---  # 0.9-047
      #self.bat_dock = QDockWidget("3D workshop", self)  0.9-047-1 removed
      #self.bat_dock.setObjectName("BatRunnerDock")         0.9-047-1 removed

        # 0.9-041 implementing a drop down menu, to begin with for credit refill
      #menubar = self.menuBar()                             0.9-047-1 removed
        # file_menu = menubar.addMenu("&File")

        # 0.9-047: hide Help/Film-info for now to keep UI minimal
        # --- 3D workshop dock ---  # 0.9-047
        self.bat_dock = QDockWidget(WORKSHOP_LABEL, self) #0.9-049
        self.bat_dock.setObjectName("BatRunnerDock")
        self.bat_dock.setVisible(False)


        dock_widget = QWidget()
        dock_layout = QVBoxLayout(dock_widget)
        dock_layout.setContentsMargins(6, 6, 6, 6)

        # credits label – first row in the dock
        self.bat_credits_label = QLabel("Done: ? | Left: ?")
        dock_layout.addWidget(self.bat_credits_label)

        # Lista
        self.bat_list = QListWidget()
        self.bat_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.bat_list.itemDoubleClicked.connect(lambda _: self._run_selected_bat())

        # Högerklicksmeny
        self.bat_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.bat_list.customContextMenuRequested.connect(self._show_bat_context_menu)

        self._bat_context_open = False  # 0.9-027

        # Lägg listan under credits-raden
        dock_layout.addWidget(self.bat_list)

         # Knapprad – keep it minimal: Archive all + Run pending  # 0.9-047
        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 0, 0, 0)

        self.btn_archive_all = QPushButton("Archive all")
        self.btn_archive_all.setToolTip(
            "Archive video, photo scripts and images to archive_YYMMDD"
        )

        self.btn_run_pending = QPushButton("Run pending")

        # Don’t let these steal keyboard focus either – they’re mouse tools #0.9-049 
        self.btn_archive_all.setFocusPolicy(Qt.NoFocus)
        self.btn_run_pending.setFocusPolicy(Qt.NoFocus)

        # Center the buttons horizontally
        btn_layout.addStretch(1)
        btn_layout.addWidget(self.btn_archive_all)
        btn_layout.addWidget(self.btn_run_pending)
        btn_layout.addStretch(1)

        dock_layout.addWidget(self.bat_list)
        dock_layout.addWidget(btn_row)

        # 0.9-055 3.3
        self.bat_dock.setWidget(dock_widget)
        self.addDockWidget(Qt.RightDockWidgetArea, self.bat_dock)

        if IS_WIN:
            ideal = self.width() // 5.7
            min_w, max_w = 120, 360
        else:
            ideal = int(self.width() / 5.2)
            min_w, max_w = 120, 360

        saved_width = int(getattr(self, "_settings_dock_width", 0) or 0)

        if saved_width > 0:
            initial_width = max(min_w, min(max_w, saved_width))
        else:
            initial_width = max(min_w, min(max_w, ideal))

        self.bat_dock.setMinimumWidth(min_w)
        self.bat_dock.setMaximumWidth(max_w)

        try:
            self.resizeDocks([self.bat_dock], [initial_width], Qt.Horizontal)
        except Exception:
            pass

        # Spara vilket värde vi faktiskt startade med
        self._settings_dock_width = initial_width

        # Runtime-status för BAT-runnern
        self._bat_procs = {}          # path_str (.bat) -> Popen-objekt
        self._bat_manual_state = {}   # path_str (.bat) -> "aborted" (om tidigare misslyckat)
        # 0.9-083: cache för film-saldo, så vi inte kör Pascal-exet varje sekund
        self._credits_cache_info = None
        self._credits_cache_ts = 0.0


        # wire buttons  # 0.9-047
        self.btn_archive_all.clicked.connect(self._archive_all)
        self.btn_run_pending.clicked.connect(self._run_all_pending)
        # (Restore finns kvar via högerklicksmenyn, ingen knapp här längre)
        # (BAT-knappen i huvudfönstret + Ctrl+B styr dockens synlighet)

        # När docken ändrar synlighet, hantera i en säker metod
        self.bat_dock.visibilityChanged.connect(self._on_bat_visibility_changed)

        # keyboard shortcuts
        self.shortcut_bat_refresh = QShortcut(Qt.Key_F5, self)
        self.shortcut_bat_refresh.activated.connect(self._refresh_bat_list)

        self.shortcut_bat_run = QShortcut(Qt.Key_Return, self)
        self.shortcut_bat_run.activated.connect(self._run_selected_bat)

        self.shortcut_toggle_dock = QShortcut(Qt.CTRL | Qt.Key_B, self)
        self.shortcut_toggle_dock.activated.connect(
            lambda: self.bat_dock.setVisible(not self.bat_dock.isVisible())
        )
        # 0.9-059 Gary/Arne: view current BAT text with G
        self.shortcut_view_bat = QShortcut(QKeySequence("G"), self)
        self.shortcut_view_bat.setContext(Qt.WidgetWithChildrenShortcut)
        self.shortcut_view_bat.activated.connect(self._show_selected_bat_text)


        # Auto-refresh timer (bara när dockan är synlig)
        self.bat_refresh_timer = QTimer(self)
        self.bat_refresh_timer.setInterval(1000)  # 1s
        self.bat_refresh_timer.timeout.connect(self._bat_refresh_tick) #0.9-021 

        # initial fill
        self._refresh_bat_list()


        self.info_label = QLabel("Open a video with Ctrl+O")
        self.info_label.setAlignment(Qt.AlignCenter)

        # Ctrl+O – öppna video
        self.shortcut_open = QShortcut(Qt.CTRL | Qt.Key_O, self)
        self.shortcut_open.activated.connect(self.open_video_dialog)

        # Ctrl+H – öppna home3d_photos_defaults-dialogen
        self.shortcut_home3d = QShortcut(Qt.CTRL | Qt.Key_H, self)
        self.shortcut_home3d.activated.connect(self.edit_home3d_photos_defaults)

        # --- Media Player Setup ---
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_view.video_item)
        self.player.setLoops(1)

        self.player.mediaStatusChanged.connect(self.handle_media_status)

        #0.9-005 -7
        self.player.positionChanged.connect(self._maybe_snap_position)


        # --- Variables ---
        self.video_path = None
        self.proxy_path = None

        #self.fps = 24.0 self detect further down 0.89-001
        self.current_frame = 0  #0.9-051 not changing anyghing here now even thogh I suspect this one is redundant when Open Video already does this
        #self.frame_ms = 1000.0 / self.fps  # skrivs över vid load_video() #0.9-002 -2

        # i __init__, med övriga variabler  0.9.002a -1
        #                                   Ersätter hela ovanstående mess, lät self.current_frame = 0 vara kvar dubblerad
        #0.9-005a -1
        self.fps = 24.0
        self._update_frame_ms()          # om du har hjälparen
        self.current_frame = 0
        self.paused = True
        self._snap_guard = False
        self.player.positionChanged.connect(self._maybe_snap_position)


        self.paused = True
        self.selection = []
        #self.use_proxy = True
        self.use_proxy = True # 0.9-071-no_proxy
        self.is_insv_source = False # 0.9-087

                           
        #.89.005 -1:
        self.left_frame = None
        self.right_frame = None
        self._export_done_for_current_video = False  # 0.9-050: status for L/R display
        self.fps = 24.0           # default; will be overwritten on load_video()
        self.video_path = None
        self.proxy_path = None


        if video_path:
            self.load_video(video_path)


        # 0.9-048 When True, update_label() will stay away from info_label (proxy work etc.)
        self._busy_label = False
        # --- Timer for UI update ---
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_label)
        self.timer.start(100)

        # --- 0.89-002 UX Grace / 0.9-057 layout ---
        # 0.9-067
        clip = os.path.basename(video_path) if video_path else None
        self.setWindowTitle(self._make_window_title(clip))

        self.info_label.setText(
            "SPACE Play/Pause | ←/→ Step | S/D Mark | A/F Check | Click direction | E Export | T tag+export | Ctrl-H settings"
        )

        # Rad direkt under videon: info + Open video + Tail 360 på samma höjd
        top_row = QWidget()
        top_layout = QHBoxLayout(top_row)
        top_layout.setContentsMargins(6, 4, 12, 4)
        top_layout.setSpacing(12)

        # Open video-knapp (samma som Ctrl+O)
        self.btn_open_video = QPushButton("Open video")
        self.btn_open_video.setToolTip("Open a video file (same as Ctrl+O)")
        self.btn_open_video.setFocusPolicy(Qt.NoFocus)
        self.btn_open_video.clicked.connect(self.open_video_dialog)

        # Tail 360-knappen (f.d. BAT runner / 3D workshop)
        self.btn_toggle_bat_main = QPushButton(WORKSHOP_LABEL)
        self.btn_toggle_bat_main.setObjectName("BatRunnerButton")
        self.btn_toggle_bat_main.setCheckable(True)
        self.btn_toggle_bat_main.setToolTip("Show/hide Tail 360 (same as Ctrl+B)")
        self.btn_toggle_bat_main.setFocusPolicy(Qt.NoFocus)

        # Gemensam form på knappar + Tail alltid grön
        self.btn_open_video.setStyleSheet(self._button_base_style)
        self.btn_toggle_bat_main.setStyleSheet(
            self._button_base_style + """
            QPushButton#BatRunnerButton {
                background-color: #80CBC4;   /* dämpad turkos, lite "electric" */
                color: #00332F;              /* mörk, lugn text */
            }
        """
        )
        #--------------------------------------------------------------------------
        # Layout: info till vänster, knappar till höger
        top_layout.addStretch(1)           #centering attempt 0.9-056
        top_layout.addWidget(self.info_label)
        top_layout.addStretch(1)
        top_layout.addWidget(self.btn_open_video)
        top_layout.addSpacing(8)
        top_layout.addWidget(self.btn_toggle_bat_main)

        container.layout().addWidget(top_row)

        # Legend-raden: egen rad, centrerad och nedtonad
        self.legend_label = QLabel(
            "   SPACE play/pause  ← → step   S D set   A F check   Click direction of movement   E export   T tag and export   Ctrl-H settings"
        )
        self.legend_label.setAlignment(Qt.AlignCenter)
        self.legend_label.setStyleSheet("color: #888888;")
        container.layout().addWidget(self.legend_label)


        # Connect the workshop button to dock visibility
        self.btn_toggle_bat_main.clicked.connect(
            lambda checked: self.bat_dock.setVisible(checked)
        )

#161000
#182105
        # Ensure the main window grabs keyboard focus
        self.setFocusPolicy(Qt.StrongFocus)
        self.video_view.setFocusPolicy(Qt.NoFocus)
#/182105
        self._snap_guard = False
        self.player.positionChanged.connect(self._maybe_snap_position)

    def _show_bat_context_menu(self, pos):
        """Contextmeny för BAT-listan: Run / Edit from VR."""
        item = self.bat_list.itemAt(pos)
        # Om inget är valt men vi högerklickar på en rad → välj den
        if item and not self.bat_list.selectedItems():
            item.setSelected(True)

        items = self.bat_list.selectedItems()
        if not items and not item:
            return  # inget att jobba med

        menu = QMenu(self)

        act_run = menu.addAction("Run")
        act_restore = menu.addAction("Edit from VR")

        # 0.9-081 --- Edit from source: nu även för VR_...bat ---
        can_restore = False
        if len(items) == 1:
            path_str = items[0].data(Qt.UserRole)
            if path_str:
                p = Path(path_str)
                kind, _ = self._bat_kind_and_base(p)
                name = p.name
                # source = gamla Film_... / Video_...
                # VR_... = nya direkta VR-skript
                if kind == "source" or name.startswith("VR_"):
                    can_restore = True
        act_restore.setEnabled(can_restore)


        # Visa menyn vid muspositionen
        global_pos = self.bat_list.mapToGlobal(pos)

        # Markera att context-menyn är öppen
        self._bat_context_open = True
        try:
            chosen = menu.exec(global_pos)
        finally:
            # Oavsett vad som händer: menyn är nu stängd
            self._bat_context_open = False
        if chosen is None:
            return

        if chosen == act_run:
            self._run_selected_bat()
        elif chosen == act_restore and can_restore:
            self._edit_selected_from_bat()
        elif chosen == act_merge and can_merge:
            self._merge_selected_source_scripts()

    #0.9-042, 0.9-084 disabled
    def _show_credits_dialog(self):
        """
        0.9-084: ersatt med enkel info om 'Used'.

        Inget home3d_photos, inga externa credits längre.
        """
        used = int(getattr(self, "_used_counter", 0) or 0)
        text = (
            "PulfrichVR credits-system\n"
            "-------------------------\n\n"
            "Det gamla externa credits-systemet (home3d_photos.exe)\n"
            "används inte längre.\n\n"
            f"Aktuell vägmätare (Used): {used}\n"
        )
        try:
            QMessageBox.information(self, "PulfrichVR info", text)
        except Exception:
            print(text)

    # Hämta film-saldo från home3d_photos.exe  0.9-035 , 0.9-084 function disabled
    def _get_credits_info(self):
        """
        0.9-084: det gamla extern-credits-systemet är borttaget.

        Den här stubben finns kvar endast för bakåtkompatibilitet.
        Vi använder inte längre home3d_photos.exe här.
        """
        try:
            used = int(getattr(self, "_used_counter", 0) or 0)
        except Exception:
            used = 0

        # Vi fusk-fyller ett 'info'-paket så gamla anrop inte brakar.
        return {
            "instance_id": "",
            "credits_total": 0,
            "credits_used": used,
            "credits_left": 0,
        }

    #0.9-042, 0.9-084 disabled
    def _add_credits_via_pascal(self, delta: int) -> bool:
        """
        0.9-082: gammal väg för att köpa/ladda film via home3d_photos.exe är borttagen.

        Vi gör ingenting här, returnerar bara False för att signalera 'ingen åtgärd'.
        """
        return False

    #0.9-042, 0.9-084 disabled
    def _prompt_add_credits(self):
        """
        0.9-084: credits-systemet är borttaget i den här versionen.
        Bara den interna 'used'-räknaren finns kvar.
        """
        try:
            QMessageBox.information(
                self,
                "Credits not used",
                "Det gamla credits-systemet är borttaget.\n\n"
                "PulfrichVR använder nu bara en intern vägmätare (Used)."
            )
        except Exception:
            pass

    #0.9-020
    def _on_bat_visibility_changed(self, visible: bool):
        if visible:
            try:
                self._refresh_bat_list()
            except RuntimeError:
                pass

            # Håll knappen i videofönstret i sync
            try:
                if hasattr(self, "btn_toggle_bat_main") and self.btn_toggle_bat_main is not None:
                    self.btn_toggle_bat_main.setChecked(True)
            except RuntimeError:
                pass

            # Starta auto-refresh
            try:
                if hasattr(self, "bat_refresh_timer"):
                    self.bat_refresh_timer.start()
            except RuntimeError:
                pass
        else:
            # Synk huvudknappen
            try:
                if hasattr(self, "btn_toggle_bat_main") and self.btn_toggle_bat_main is not None:
                    self.btn_toggle_bat_main.setChecked(False)
            except RuntimeError:
                pass

            # Stoppa auto-refresh
            try:
                if hasattr(self, "bat_refresh_timer"):
                    self.bat_refresh_timer.stop()
            except RuntimeError:
                pass



    #0.9-055 3.1)
    def _load_settings(self):
        """
        Läs home3dframes_settings.txt

        Rad 1: last_dir
        Rad 2: cmd_mode ("k" / "c")
        Rad 3: dock_width (valfri, int)
        Rad 4: used_counter (valfri, int)
        """
        from pathlib import Path

        movies_dir = QStandardPaths.writableLocation(QStandardPaths.MoviesLocation)
        if not movies_dir:
            movies_dir = str(Path.home())

        default_dir = str(RUNTIME_DIR)
        default_cmd_mode = "c"

        path = runtime_path("home3dframes_settings.txt")
        last_dir = default_dir
        cmd_mode = default_cmd_mode
        dock_width = 0
        used_counter = 0   # 0.9-084: intern vägmätare, MÅSTE initieras här

        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []

        # Rad 1: senast använda katalog
        if len(lines) >= 1 and lines[0].strip():
            candidate = lines[0].strip()
            if os.path.isdir(candidate):
                last_dir = candidate

        # Rad 2: cmd_mode ("k" / "c")
        if len(lines) >= 2 and lines[1].strip().lower() in ("k", "c"):
            cmd_mode = lines[1].strip().lower()

        # Rad 3: dock-bredd (valfri)
        if len(lines) >= 3 and lines[2].strip().isdigit():
            try:
                dw = int(lines[2].strip())
                if dw > 0:
                    dock_width = dw
            except Exception:
                dock_width = 0

        # Rad 4: used-counter (valfri) 0.9-084
        if len(lines) >= 4 and lines[3].strip().isdigit():
            try:
                used_counter = int(lines[3].strip())
            except Exception:
                used_counter = 0

        # 0.9-087: rad 5 = insv-warning-flagga (1=visa, 0=visa inte)
        show_insv_warning = True
        if len(lines) >= 5:
            v = lines[4].strip()
            if v == "0":
                show_insv_warning = False
            elif v == "1":
                show_insv_warning = True

        self._settings_last_dir = last_dir
        self._settings_cmd_mode = cmd_mode
        self._settings_dock_width = dock_width
        self._used_counter = used_counter
        self._show_insv_warning_flag = show_insv_warning #0.9-087


    # 0.9-084
    def _save_settings(self):
        """Skriv hem inställningar (last_dir, cmd_mode, dock_width, used_counter)."""
        path = runtime_path("home3dframes_settings.txt")
        last_dir = getattr(self, "_settings_last_dir", "") or ""
        cmd_mode = getattr(self, "_settings_cmd_mode", "c") or "c"
        dock_width = int(getattr(self, "_settings_dock_width", 0) or 0)
        used_counter = int(getattr(self, "_used_counter", 0) or 0)
        show_insv_warning = bool(getattr(self, "_show_insv_warning_flag", True)) #0.9-087


        lines = [
            last_dir,
            cmd_mode,
            str(dock_width) if dock_width > 0 else "",
            str(used_counter),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _bat_to_sh(self, bat_path: Path) -> Path:
        """
        Convert a Windows .bat (stereo script) to a Linux .sh next to it.
        Non-destructive: leaves the .bat untouched.
        Rules:
          - '@rem ...'       -> '# ...'
          - 'ffmpeg(.exe)...'-> '{ffmpeg} ...' with '\'->'/', '-update true'->'-update 1'
          - '@del file'      -> 'rm -f file' with '\'->'/'
          - unknown lines    -> '# original: <line>'
        """
        ff = get_ffmpeg_cmd()
        bat_text = bat_path.read_text(encoding="utf-8", errors="ignore")
        lines = bat_text.splitlines()

        out = []
        out.append("#!/usr/bin/env bash")
        out.append("set -euo pipefail")

        for raw in lines:
            line = raw.strip()
            if not line:
                out.append("")
                continue

            low = line.lower()
     
            # comments
            if low.startswith("@rem") or low.startswith("rem "):
                out.append("# " + line.lstrip("@").lstrip()[4:].strip())
                continue

            # delete temp files
            if low.startswith("@del ") or low.startswith("del "):
                path_part = line.split(" ", 1)[1].strip()
                path_part = path_part.replace("\\", "/")
                out.append(f'rm -f "{path_part}"')
                continue

            # ffmpeg calls
            if low.startswith("ffmpeg") or low.startswith("ffmpeg.exe"):
                body = line
                # replace leading ffmpeg(.exe) with your ffmpeg cmd
                if body.lower().startswith("ffmpeg.exe"):
                    body = body[len("ffmpeg.exe"):].lstrip()
                elif body.lower().startswith("ffmpeg"):
                    body = body[len("ffmpeg"):].lstrip()
                body = body.replace("\\", "/")
                body = body.replace("-update true", "-update 1")
                out.append(f'{ff} {body}')
                continue
      
            # ignore pure echo off, etc., but keep as comment if unsure
            if low.startswith("@echo") or low == "echo off":
                out.append(f"# {line}")
                continue

            # keep unknown as comment (safety)
            out.append(f"# original: {line}")

        sh_path = bat_path.with_suffix(".sh")
        sh_path.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")

        # chmod +x
        st = os.stat(sh_path)
        os.chmod(sh_path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        print(f"[CONVERT] {bat_path.name} -> {sh_path.name}")
        return sh_path

    #0.9-009 3)
    from PySide6.QtWidgets import QMessageBox


    def _make_photos_cmd(self) -> str | None:
        #0.9-009b
        override = os.environ.get("HOME3DPHOTOS_CMD")
        #print("override = ", override)
        if override:
            return override

        expected = runtime_path("home3d_photos.exe")
        #print("expected = ", expected)

        if IS_WIN:
            # Windows: kräver exe i runtime root
            if expected.exists():
                return str(expected)

            msg = (
                "home3d_photos.exe hittas inte i programkatalogen.\n\n"
                f"Förväntad plats:\n  {expected}\n\n"
                "Lägg home3d_photos.exe bredvid Home3dFrames.exe."
            )
            print("❌ " + msg.replace("\n", " "))
            if hasattr(self, "info_label"):
                self.info_label.setText("❌ home3d_photos.exe saknas i root")
            try:
                QMessageBox.critical(self, "home3d_photos.exe saknas", msg)
            except Exception:
                pass
            return None

        else:
            # Linux/Mint: kör via wine (om exe finns i root)
            if expected.exists():
                if shutil.which("wine") is None:
                    msg = (
                        "Hittade home3d_photos.exe i root men wine saknas.\n\n"
                        "Installera wine eller sätt env HOME3DPHOTOS_CMD "
                        "till ett annat kommando."
                    )
                    print("❌ " + msg.replace("\n", " "))
                    if hasattr(self, "info_label"):
                        self.info_label.setText("❌ wine saknas (behövs för home3d_photos.exe)")
                    try:
                        QMessageBox.critical(self, "Wine saknas", msg)
                    except Exception:
                        pass
                    return None

                # wrap with wine, och citera sökvägen
                return f'wine "{expected}"'

            # Om exe inte finns i root på Linux: fall tillbaka till wine i PATH
            if shutil.which("wine") is not None:
                return "wine home3d_photos.exe"

            # sista utväg: tydligt fel
            msg = (
                "Kan inte köra home3d_photos: varken home3d_photos.exe i root "
                "eller wine finns.\n\n"
                "Lägg home3d_photos.exe bredvid home3d_frames.py "
                "och installera wine, eller sätt HOME3DPHOTOS_CMD."
            )
            print("❌ " + msg.replace("\n", " "))
            if hasattr(self, "info_label"):
                self.info_label.setText("❌ home3d_photos kan inte köras (wine/exe saknas)")
            try:
                QMessageBox.critical(self, "home3d_photos kan inte köras", msg)
            except Exception:
                pass
            return None
    #0.9-004 -1

    def _ffmpeg_cmd(self) -> str:
        return os.environ.get("FFMPEG_CMD", "ffmpeg")


    # 0.9-005a -5
    def _maybe_snap_position(self, ms):
        # vid uppspelning: uppdatera current_frame från tiden (ok)
        if not getattr(self, "paused", True):
            self.current_frame = self._pos_to_frame(int(ms))
            return
        # vid paus: håll oss på rutfördelningen, men ändra inte current_frame här
        if self._snap_guard:
            return
        center = self._frame_to_pos(int(self.current_frame))  #0.9-005c -4
        if abs(int(ms) - center) >= 1:
            self._snap_guard = True
            try:
                self.player.setPosition(center)
            finally:
                self._snap_guard = False


    #0.9-005c -1
    def _update_frame_ms(self):
        self.frame_ms = 1000.0 / max(1e-6, float(self.fps))

    def _pos_to_frame(self, pos_ms: int) -> int:
        # golva stabilt (liten epsilon mot fp-brus)
        return int(math.floor((float(pos_ms) + 1e-9) / max(1e-6, self.frame_ms)))

    def _frame_to_pos(self, frame_idx: int) -> int:
        # Gå till mitten av rutan → undvik exakt-gräns-problem
        return int(round((float(frame_idx) + 0.5) * self.frame_ms))

    # 0.9-087-1
    def _is_insv_path(self, path: str) -> bool:
        """Return True om sökvägen pekar på en .insv-video."""
        return str(path).lower().endswith(".insv")

    # 0.9-061: tagg för både VID_... och godtyckliga filnamn
    def _make_video_tag(self, path: str):
        """
        Skapa en grund-tagg från videofilens namn.

        För Insta360-klipp i formatet
            VID_YYYYMMDD_hhmmss_00_NNN.ext
        → 'YYMMDD_NNN' (som tidigare) och returnerar (tag, True).

        För alla andra filnamn:
          * Ta filnamns-stammen (utan .ext)
          * Normalisera:
                - mellanslag → '_'
                - svenska tecken å/ä/ö/Å/Ä/Ö → a/a/o/A/A/O
                - ta bort övriga "konstiga" tecken
          * Trunka till max 12 tecken
        → (tag, False).
        """
        base = Path(path).stem

        # Försök först tolka som klassiskt Insta360-VID_...
        parts = base.split("_")
        if (
            len(parts) >= 4
            and parts[0].upper() == "VID"
            and len(parts[1]) == 8
            and parts[1].isdigit()
        ):
            date_str = parts[1][2:]  # 20251031 -> "251031"
            clip_raw = parts[-1]     # t.ex. "040", "040(1)", "040(2)"

            # matcha t.ex. "040" eller "040(1)" → grupp1="040", grupp2="1" eller None
            m = re.match(r"^(\d+)(?:\((\d+)\))?", clip_raw)
            if m:
                main_num = m.group(1)
                copy_idx = m.group(2)
                if copy_idx:
                    clip_tag = f"{main_num}-{copy_idx}"
                else:
                    clip_tag = main_num
            else:
                clip_tag = clip_raw

            # Klassiskt fall: Insta360-format → behåll gamla beteendet
            return f"{date_str}_{clip_tag}", True

        # --- Godtyckliga filnamn: bygg en kort, "CLI-säker" tagg ---
        s = base.strip()

        # 1) mellanslag -> "_"
        s = re.sub(r"\s+", "_", s)

        # 2) svenska tecken, hygglig translitterering
        translation_table = str.maketrans({
            "å": "a", "ä": "a", "ö": "o",
            "Å": "A", "Ä": "A", "Ö": "O",
        })
        s = s.translate(translation_table)

        # 3) behåll bara a–z, A–Z, 0–9, _ och -
        s = "".join(ch for ch in s if ch.isalnum() or ch in "_-")

        # 4) Om allt försvann → nödfall
        if not s:
            s = "video"

        # 5) Trunka till max 12 tecken (kort men igenkännbart)
        tag = s[:12]
        return tag, False
    #0.9-005a -3
    def _jump_to_frame(self, frame_idx: int):
        try:
            if not self.player.isAvailable():
                return
            if not self.paused:
                self.player.pause(); self.paused = True
         
            duration_ms = int(self.player.duration())
            total_frames = max(1, self._pos_to_frame(max(0, duration_ms - 1)) + 1)

            idx = max(0, min(int(frame_idx), total_frames - 1))
            pos_ms = self._frame_to_pos(idx)

            self._snap_guard = True
            try:
                self.player.setPosition(pos_ms)
            finally:
                self._snap_guard = False

            self.current_frame = idx
            print(f"Jumped to frame: {idx}  (pos={pos_ms} ms)")
        except Exception as e:
            print(f"[jump_to_frame] Error: {e}")

        def _handle_media_status(self, status):
            from PySide6.QtMultimedia import QMediaPlayer
            # When media signals EndOfMedia -> pause and stay at last frame
            if status == QMediaPlayer.EndOfMedia:
                try:
                    self.player.pause()
                except Exception:
                    pass
                self.paused = True



    # -----------------------------
    # Core: Load or create proxy
    # -----------------------------
    def load_video(self, path):
        if not os.path.exists(path):
            self.info_label.setText("❌ File not found.")
            return

        # Ny video → vi betraktar export-status som "inte gjord" 0.9-050
        # Ny video → starta från “noll”: inga L/R-val, ingen riktning, ingen export-status 0.9-051
        self._export_done_for_current_video = False
        self.left_frame = None
        self.right_frame = None
        self.current_frame = 0

        # Nollställ riktningen i overlayn
        overlay = getattr(self.video_view, "overlay", None)
        if overlay is not None:
            try:
                overlay.set_line_x(None)
            except TypeError:
                overlay.line_x = None
                overlay.update()
        # Även den enklare spegling vi har i viewn
        self.video_view.line_x = None

        # Absolut sökväg + fps
        self.video_path = os.path.abspath(path)
        self.fps = self.detect_fps(self.video_path)
        self._update_frame_ms()

        # Diskret notis vid native .insv ("round tour raw")  # 0.9-087
        if self.video_path.lower().endswith(".insv"):
            self._show_insv_roundtour_warning()

        # Är detta en rå .insv-fil?  (påverkar proxy och export) #0.9-087
        self.is_insv_source = self._is_insv_path(self.video_path)


        # 0.9-087: proxy-namn inkluderar filtyp (.mov/.insv) för att undvika kollisioner
        self.proxy_path = self._make_proxy_path(self.video_path)

        if self.use_proxy:
            proxy_exists = os.path.exists(self.proxy_path)
            is_insv = self.video_path.lower().endswith(".insv")
            need_rebuild = True

            try:
                src_stat = os.stat(self.video_path)
            except OSError:
                src_stat = None

            if not is_insv:
                # Endast för "vanliga" videor försöker vi återanvända proxy
                if proxy_exists and src_stat is not None:
                    try:
                        proxy_stat = os.stat(self.proxy_path)
                        # Om proxyn är minst lika ny som videon: återanvänd
                        if proxy_stat.st_mtime >= src_stat.st_mtime:
                            need_rebuild = False
                    except OSError:
                        need_rebuild = True
                elif proxy_exists and src_stat is None:
                    # Konstigt läge, men om vi har en proxy → återanvänd
                    need_rebuild = False

            # För insv är need_rebuild alltid True → vi bygger om proxyn
            if need_rebuild:
                self._busy_label = True
                if hasattr(self, "info_label") and self.info_label is not None:
                    self.info_label.setText("⚙️ Creating proxy…")
                    self.info_label.repaint()
                    QApplication.processEvents()

                self.create_proxy(self.video_path, self.proxy_path)

                self._busy_label = False
                if hasattr(self, "info_label") and self.info_label is not None:
                    self.info_label.setText(
                        f"🎬 Using proxy: {os.path.basename(self.proxy_path)}"
                    )
            else:
                self._busy_label = True
                if hasattr(self, "info_label") and self.info_label is not None:
                    self.info_label.setText(
                        f"🎬 Loading proxy: {os.path.basename(self.proxy_path)}"
                    )
                    self.info_label.repaint()
                    QApplication.processEvents()
                self._busy_label = False

            # Din fps-override för insv från proxyn kan ligga kvar här:
            if self.video_path.lower().endswith(".insv"):
                try:
                    proxy_fps = self.detect_fps(self.proxy_path)
                    if proxy_fps > 0:
                        self.fps = proxy_fps
                        self._update_frame_ms()
                        print(f"[Proxy FPS] Using {proxy_fps:.3f} fps from proxy for insv.")
                except Exception as e:
                    print(f"[Proxy FPS] Failed to detect from proxy: {e}")

            self.start_player(self.proxy_path)
        else:
            self.start_player(self.video_path)

        # Uppdatera fönstertitel baserat på aktuell video  # 0.9-057
        # 0.9-067
        clip = os.path.basename(self.video_path) if self.video_path else None
        self.setWindowTitle(self._make_window_title(clip))


    # 0.9-091
    def detect_fps(self, video_path):
        """Detect FPS using ffprobe; fallback to DEFAULT_FPS if unavailable."""
        try:
            cmd = [
                get_ffprobe_cmd(),
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=r_frame_rate",
                "-of", "default=nokey=1:noprint_wrappers=1",
                video_path,
            ]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            rate = result.stdout.strip()
            if rate and "/" in rate:
                num, den = map(float, rate.split("/"))
                fps = num / den if den else float(num)
                if fps > 0:
                    print(f"[Auto FPS] Detected {fps:.3f}")
                    return fps
        except Exception as e:
            print(f"[Auto FPS] Detection failed: {e}")
        return DEFAULT_FPS

    #0.89-007 step1
    def detect_source_width(self, video_path) -> int:
        """Return video stream width via ffprobe; fallback to 7680 on failure."""
        try:
            cmd = [
                get_ffprobe_cmd(), "-v", "error",  #0.9-008 5): changed from "ffprobe"
                "-select_streams", "v:0",
                "-show_entries", "stream=width",
                "-of", "default=nokey=1:noprint_wrappers=1",
                video_path,
            ]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            w = int(result.stdout.strip())
            if w > 0:
                print(f"[Auto WIDTH] Detected {w}")
                return w
        except Exception as e:
            print(f"[Auto WIDTH] Detection failed: {e}")
        return 7680


    # 0.9-087: unik proxy per källtyp (.mov, .insv, ...)
    def _make_proxy_path(self, src: str) -> str:
        """
        Returnerar en unik proxy-sökväg för en given videofil.
        Exempel:
          VID_..._204.mov  -> proxy_cache/VID_..._204_mov_proxy.mp4
          VID_..._204.insv -> proxy_cache/VID_..._204_insv_proxy.mp4
        """
        proxy_dir = runtime_path("proxy_cache")
        os.makedirs(proxy_dir, exist_ok=True)

        p = Path(src)
        base = p.stem
        ext = p.suffix.lower().lstrip(".") or "vid"  # t.ex. "mov", "insv"

        proxy_name = f"{base}_{ext}_proxy.mp4"
        return str(proxy_dir / proxy_name)

    # 0.9-087
    def create_proxy(self, src, dst):
        """
        Skapar en videoproxy för snabb uppspelning / framesteppning.

        - Vanliga 360-klipp (.mp4/.mov/.mkv):
            skala ned till 2880x1440.

        - .insv ("round tour raw"):
            använd båda videoströmmarna (två linser),
            hstacka dem, kör v360 dfisheye -> equirect, skala till 2880x1440.
        """
        src_lower = src.lower()
        ff = get_ffmpeg_cmd()

        # Rensa ev. gammal proxy först (skadar inte, hjälper oss att debugga)
        try:
            if os.path.exists(dst):
                os.remove(dst)
        except Exception as e:
            print(f"[PROXY] could not remove existing {dst}: {e}")

        if src_lower.endswith(".insv"):
            # Försök först med "stereo"-pipeline: två videoströmmar (0:v:0 + 0:v:1)
            # → hstack → v360 dfisheye:e → scale → [eq]
            # Tog bort rgb24 härifrån. rgb24 är viktig för bat-skripten men inte här i redigeringslandet.
            filter_complex_stereo = (
                "[0:v:0]format=[f0];"
                "[0:v:1]format=[f1];"
                "[f0][f1]hstack=inputs=2[df];"
                "[df]v360=dfisheye:e:"
                "ih_fov=198:iv_fov=198:"
                "yaw=0:pitch=0:roll=0,"
                "scale=2880:1440[eq]"
            )

            cmd = [
                ff, "-y",
                "-i", src,
                "-filter_complex", filter_complex_stereo,
                "-map", "[eq]",
                "-c:v", "libsvtav1",
                "-an",
                dst,
                "-hide_banner",
            ]

            try:
                import shlex
                print("[PROXY insv stereo] cmd:", " ".join(shlex.quote(c) for c in cmd))
            except Exception:
                print("[PROXY insv stereo] cmd:", cmd)

            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            if result.returncode != 0:
                # Fallback: om filen bara har en stream eller något annat strular
                print("[PROXY insv] stereo-pipeline failed, falling back to single-stream v360.")
                vf_chain = (
                    "v360=dfisheye:e:"
                    "ih_fov=198:iv_fov=198:"
                    "yaw=0:pitch=0:roll=0,"
                    "scale=2880:1440"
                )
                cmd2 = [
                    ff, "-y",
                    "-i", src,
                    "-vf", vf_chain,
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "25",
                    "-an",
                    dst,
                    "-hide_banner",
                ]
                try:
                    print("[PROXY insv fallback] cmd2:",
                          " ".join(shlex.quote(c) for c in cmd2))
                except Exception:
                    print("[PROXY insv fallback] cmd2:", cmd2)
                subprocess.run(cmd2, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        else:
            # Vanlig proxy (som tidigare): bara skala ned
            vf_chain = "scale=2880:1440"
            cmd = [
                ff, "-y",
                "-i", src,
                "-vf", vf_chain,
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "25",
                "-an",
                dst,
                "-hide_banner",
            ]
            try:
                import shlex
                print("[PROXY normal] cmd:", " ".join(shlex.quote(c) for c in cmd))
            except Exception:
                print("[PROXY normal] cmd:", cmd)

            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def create_proxy_insv(self, src, dst):
        """
        0.9-087
        Skapa en equirect-proxy från en rå .insv med två videoströmmar:

          [0:v:0] + [0:v:1] -> hstack (df)
          df -> v360 dfisheye:e -> equirect
          equirect -> scale=2880:1440

        Antagande: två videoströmmar (v:0 och v:1) med samma upplösning.
        """
        ff = get_ffmpeg_cmd()
        filter_complex = (
            "[0:v:0]select=eq(n\\,0),setpts=PTS-STARTPTS[f0a];"
            "[0:v:1]select=eq(n\\,0),setpts=PTS-STARTPTS[f0b];"
            "[f0a][f0b]hstack=inputs=2[df];"
            "[df]v360=dfisheye:e:"
            "ih_fov=198:iv_fov=198:"
            "yaw=0:pitch=0:roll=0,"
            "scale=2880:1440[vout]"
        )

        cmd = [
            ff, "-y",
            "-i", src,
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "25",
            "-an", dst,
            "-hide_banner",
        ]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


    # -----------------------------
    # Core player and controls
    # -----------------------------    
    def start_player(self, file_path):
        self.player.setSource(QUrl.fromLocalFile(file_path))
        # Start paused at t=0
        self.player.pause()
        self.paused = True  #0.89-009 -1
        # Seek to 0 after backend is ready
        QTimer.singleShot(0, lambda: self.player.setPosition(0))


#181912
    def keyPressEvent(self, event):
        key = event.key()

        # --- SPACE: play / pause toggle ---
        if key == Qt.Key_Space:
            if self.paused:
                self.player.play()
                self.paused = False
            else:
                self.player.pause()
                self.paused = True
            return

        # --- LEFT / RIGHT ARROWS: frame stepping ---
        elif key == Qt.Key_Left:
            self.step_frame(-1)
        elif key == Qt.Key_Right:
            self.step_frame(1)

        # --- S = left eye (start) ---
        # 0.9-005a -4
        elif key == Qt.Key_S:
            if not self.paused:
                self.player.pause(); self.paused = True
            self.left_frame = int(self.current_frame)
            # 0.9-057: ny L-markering = inte längre "klar"
            self._export_done_for_current_video = False
            print(f"[Left eye] Frame marked: {self.left_frame}")
            return

        elif key == Qt.Key_D:
            if not self.paused:
                self.player.pause(); self.paused = True
            self.right_frame = int(self.current_frame)
            # 0.9-057: ny R-markering = inte längre "klar"
            self._export_done_for_current_video = False
            print(f"[Right eye] Frame marked: {self.right_frame}")
            return

        #0.9-006b
        elif key == Qt.Key_A:
            lf = getattr(self, "left_frame", None)
            if lf is None:
                print("⚠ Left frame not set."); return
            self._jump_to_frame(int(lf))
            print(f"Jumped to Left frame: {int(lf)}")
            return

        elif key == Qt.Key_F:
            rf = getattr(self, "right_frame", None)
            if rf is None:
                print("⚠ Right frame not set."); return
            self._jump_to_frame(int(rf))
            print(f"Jumped to Right frame: {int(rf)}")
            return


    # --- E = export selection ---  # 0.9-089
        elif key == Qt.Key_E:
            if hasattr(self, "left_frame") and hasattr(self, "right_frame"):
                self.export_selection("")  # no extra tag
            else:
                print("⚠️ Please mark both S (left) and D (right) frames before exporting.")

        # --- T = export selection with tag ---
        elif key == Qt.Key_T:
            # Grundkoll: L och R måste finnas innan vi ens visar dialogen
            lf = getattr(self, "left_frame", None)
            rf = getattr(self, "right_frame", None)
            if lf is None or rf is None:
                msg = "⚠️ Please mark both S (left) and D (right) frames before exporting with tag."
                print(msg)
                if hasattr(self, "info_label") and self.info_label is not None:
                    self.info_label.setText(msg)
                return

            # En enda frågeruta för taggen (kan vara tom, det är ok)
            text, ok = QInputDialog.getText(self, "Image tag", "Image tag (optional):")
            if not ok:
                return

            # Nya export_selection tar själv hand om VR_...bat + 360TB + LR
            self.export_selection(text)
            return

        else:
            super().keyPressEvent(event)


#/181912

    # 0.9-081: återställ stereo-läge från VR_...bat
    def _edit_vr_script(self, bat_path_str: str):
        """
        Läser VR_META i ett VR_*.bat och:

          - laddar videon
          - sätter left_frame / right_frame
          - sätter horisontal riktning (kompasslinjen)
          - hoppar till vänsterögats frame

        Så att vi hamnar i 'redo att trycka E/T'-läget igen.
        """
        p = Path(bat_path_str)
        meta = self._extract_vr_meta_from_script(p)
        if meta is None:
            return

        video_token = meta["video"]
        left_idx    = meta["left"]
        right_idx   = meta["right"]
        dir_px      = meta["dir_px"]
        source_res  = meta["res_px"]

        # Försök lösa upp video-sökväg, liknande _edit_source_script
        video_path_fs = video_token
        try:
            if not os.path.isabs(video_token):
                candidate1 = p.parent / video_token
                candidate2 = runtime_path(video_token)

                print(f"[EDIT-VR] video token: {video_token}")
                print(f"[EDIT-VR] candidate1 (script dir): {candidate1} exists={candidate1.exists()}")
                print(f"[EDIT-VR] candidate2 (runtime dir): {candidate2} exists={candidate2.exists()}")

                if candidate1.exists():
                    video_path_fs = str(candidate1)
                elif candidate2.exists():
                    video_path_fs = str(candidate2)
        except Exception as e:
            print(f"[EDIT-VR] Exception while resolving video path for {p.name}: {e}")

        print(f"[EDIT-VR] final video_path_fs: {video_path_fs} exists={os.path.exists(video_path_fs)}")

        if not os.path.exists(video_path_fs):
            if hasattr(self, "info_label"):
                self.info_label.setText(f"❌ Video not found for {p.name}")
            return

        # Ladda videon (detta nollställer L/R/riktning etc.)
        self.load_video(video_path_fs)

        # Sätt L/R och hoppa till vänsterögat
        self.left_frame = int(left_idx)
        self.right_frame = int(right_idx)
        self._jump_to_frame(int(left_idx))

        # Sätt riktningen i overlay från dir_px/res_px
        try:
            video_rect = self.video_view.video_item.boundingRect()
            drawn_w = float(video_rect.width()) if video_rect.isValid() else float(self.video_view.viewport().width() or 1.0)
            if drawn_w > 0 and source_res > 0:
                ratio = float(dir_px) / float(source_res)
                x = max(0.0, min(drawn_w, ratio * drawn_w))
                self.video_view.line_x = x
                if hasattr(self.video_view, "overlay") and self.video_view.overlay is not None:
                    self.video_view.overlay.set_line_x(x)
                    self.video_view.overlay.update()
        except Exception as e:
            print(f"[EDIT-VR] Direction restore failed: {e}")

        # Markera att vi nu är i ett "pågående" läge (inte done)
        self._export_done_for_current_video = False

        if hasattr(self, "info_label"):
            self.info_label.setText(
                f"Restored from {p.name} | L={left_idx} R={right_idx} dir={dir_px}/{source_res}"
            )

    #0.9-005a -2
    def step_frame(self, direction: int):
        if not self.player.isAvailable():
            return
        if not self.paused:
            self.player.pause(); self.paused = True

        duration_ms = int(self.player.duration())
        total_frames = max(1, self._pos_to_frame(max(0, duration_ms - 1)) + 1)

        self.current_frame = (int(self.current_frame) + int(direction)) % total_frames
        pos_ms = self._frame_to_pos(self.current_frame)

        self._snap_guard = True
        try:
            self.player.setPosition(pos_ms)
        finally:
            self._snap_guard = False

        print(f"Stepped to frame: {self.current_frame}  (pos={pos_ms} ms)")

    def update_label(self):  # 0.9-005 -8 / 0.9-050
        try:
            # Don’t touch info_label while we’re in a “busy” state (proxy work)
            if getattr(self, "_busy_label", False):
                return

            pos_ms = int(self.player.position())
            center_ms = self._frame_to_pos(int(self.current_frame))

            # L/R state
            lf = getattr(self, "left_frame", None)
            rf = getattr(self, "right_frame", None)
            has_L = lf is not None
            has_R = rf is not None

            # Har Roland klickat in riktning?
            overlay = getattr(self.video_view, "overlay", None)
            direction_set = overlay is not None and getattr(overlay, "line_x", None) is not None

            done = getattr(self, "_export_done_for_current_video", False)

            # Bestäm färg beroende på status:
            #  - done: grön + bold
            #  - ready (L+R+dir, ej done): blå
            #  - partial (L eller R vald): bärnsten
            #  - none: grå
            if done:
                # color = "#2e7d32"   # grön (done) changing green to black 0.9-056
                color = "000000"
                bold = False        # 0.9-051 nedtoning från fetstils-True
            elif has_L and has_R and direction_set:
                color = "#1565c0"   # blå (redo för export)
                bold = False
            elif has_L or has_R:
                color = "#b26a00"   # bärnsten (på gång)
                bold = False
            else:
                color = "#999999"   # grå (inget valt än)
                bold = False

            lf_str = str(int(lf)) if has_L else "-"
            rf_str = str(int(rf)) if has_R else "-"

            style_parts = [f"color:{color}"]
            if bold:
                style_parts.append("font-weight:bold")
            style = "; ".join(style_parts)

            lr_html = f'<span style="{style}">L={lf_str} R={rf_str}</span>'
            rest_html = (
                f'<span>  |  t_center={center_ms/1000.0:0.3f}s'
                f'  |  frame={int(self.current_frame)}</span>'
            )

            self.info_label.setText(lr_html + rest_html)
        except Exception:
            pass
        
    # -----------------------------
    # Selection and export logic  0.9-012 4)
    # -----------------------------
    def save_selection(self):
        frame = int(self.player.position() / 1000.0 * self.fps)
        self.selection.append(frame)
        if len(self.selection) > 2:
            self.selection.pop(0)
        self.info_label.setText(f"Selected frames: {self.selection}")

    #0.9-091
    def save_selection(self):
        # Använd samma frame-begrepp som resten av koden
        frame = int(self.current_frame)
        # Alternativt, om du vill "mäta från tiden":
        # frame = self._pos_to_frame(int(self.player.position()))

        self.selection.append(frame)
        if len(self.selection) > 2:
            self.selection.pop(0)
        self.info_label.setText(f"Selected frames: {self.selection}")
    def edit_home3d_photos_defaults(self):
        """
        Enkel dialog för att redigera de viktigaste raderna i
        home3d_photos_defaults.txt.

        Layout (1-baserad radnumrering):

          1  tagname
          2  img1
          3  img2
          4  img3
          5  img4
          6  resolution
          7  direction
          8  alternative direction
          9  Keep temporary files
          10 create SBS files
          11 Keep right focused images
          12 create anaglyph images
          13 create single stereo images
          14 PNG mode flag (0=jpg, 1=PNG)
          15 Left side focus
          16 Right side focus
        """

        path = runtime_path("home3d_photos_defaults.txt")

        # Om filen inte finns: skapa en "klassisk" layout
        if not path.exists():
            top16 = [
                "tagname",       # 1
                "img001.png",    # 2
                "img002.png",    # 3
                ".",             # 4 (img3 – låt . betyda "tom")
                ".",             # 5 (img4)
                "7680",          # 6 resolution
                "0",             # 7 direction
                "same",          # 8 alt direction
                "n",             # 9 Keep temporary files
                "n",             #10 create SBS files
                "n",             #11 Keep right focused images
                "n",             #12 create anaglyph images
                "y",             #13 create single stereo images
                "1",             #14 PNG mode (1=PNG, 0=JPG)
                "56",            #15 Left focus
                "56",            #16 Right focus
            ]
            bottom = [
                "----------------",
                "line:\tfunction:",
                "----------------",
                "1\t tagname",
                "2\t img1",
                "3\t img2",
                "4\t img3",
                "5\t img4",
                "",
                "6\t resolution",
                "7\t direction",
                "8\t alternative direction",
                "",
                "9\t Keep temporary files",
                "10\t create SBS files",
                "11\t Keep right focused images",
                "12\t create anaglyph images",
                "13\t create single stereo images",
                "14\t PNG mode (0 = jpg mode, 1 = PNG mode)",
                "15\t Left side Right eye focus angle in pixels",
                "16\t Right side Right eye focus angle in pixels",
                "----------------",
            ]
            path.write_text(
                "\n".join(top16 + bottom) + "\n",
                encoding="utf-8"
            )

        # Läs in hela filen, behåll layouten under rad 16
        text = path.read_text(encoding="utf-8")
        all_lines = text.splitlines()

        # Se till att vi kan adressera minst 16 rader (fyll med "." vid behov)
        if len(all_lines) < 16:
            all_lines += ["." for _ in range(16 - len(all_lines))]

        top16 = all_lines[:16]
        tail_lines = all_lines[16:]

        # Små hjälpare
        def get_bool_idx(idx: int, default: bool) -> bool:
            try:
                val = top16[idx].strip().lower()
            except IndexError:
                return default
            if val.startswith("y"):
                return True
            if val.startswith("n"):
                return False
            return default

        def get_int_idx(idx: int, default: int) -> int:
            try:
                return self._safe_int(top16[idx], default)
            except Exception:
                return default

        # Hårdkodade standarder (bara lokalt här)
        DEF_KEEP_WORK  = False
        DEF_SBS        = False
        DEF_KEEP_RIGHT = False
        DEF_ANA        = False
        DEF_SINGLE     = True
        DEF_PNG_MODE   = 1
        DEF_FOCUS      = 56

        # Plocka ut nuvarande värden med tolerans för skräp/blankt
        keep_work     = get_bool_idx(IDX_KEEP_WORK,  DEF_KEEP_WORK)
        sbs_lr        = get_bool_idx(IDX_SBS,        DEF_SBS)
        keep_right    = get_bool_idx(IDX_KEEP_RIGHT, DEF_KEEP_RIGHT)
        create_ana    = get_bool_idx(IDX_ANA,        DEF_ANA)
        create_single = get_bool_idx(IDX_SINGLE,     DEF_SINGLE)

        # PNG-flagga: bara "1" betyder PNG, allt annat → JPG (0)
        try:
            raw_ff = top16[IDX_PNG_MODE].strip()
        except IndexError:
            raw_ff = ""
        ff_mode = 1 if raw_ff == "1" else 0

        left_focus_orig  = get_int_idx(IDX_LEFT_FOCUS,  DEF_FOCUS)
        right_focus_orig = get_int_idx(IDX_RIGHT_FOCUS, DEF_FOCUS)

        # --- Bygg dialogen ---
        dlg = QDialog(self)
        dlg.setWindowTitle("home3d_photos_defaults.txt")

        vbox = QVBoxLayout(dlg)
        form = QFormLayout()

        chk_keep_work = QCheckBox("Keep workfiles")
        chk_keep_work.setChecked(keep_work)
        form.addRow("Keep workfiles:", chk_keep_work)

        chk_sbs = QCheckBox("SBS images for left and right side")
        chk_sbs.setChecked(sbs_lr)
        form.addRow("SBS images:", chk_sbs)

        chk_keep_right = QCheckBox("Keep right focused side")
        chk_keep_right.setChecked(keep_right)
        form.addRow("Keep right focused side:", chk_keep_right)

        chk_ana = QCheckBox("Create anaglyph images")
        chk_ana.setChecked(create_ana)
        form.addRow("Create anaglyph images:", chk_ana)

        chk_single = QCheckBox("Create single stereo images")
        chk_single.setChecked(create_single)
        form.addRow("Create single stereo images:", chk_single)

        chk_png = QCheckBox("PNG mode (1 = PNG, 0 = JPG)")
        chk_png.setChecked(ff_mode == 1)
        form.addRow("PNG mode:", chk_png)

        def make_focus_spin(orig: int) -> QSpinBox:
            sb = QSpinBox()
            limit = max(1, int(round(abs(orig) * 1.5))) if orig != 0 else 100
            sb.setRange(-limit, limit)
            sb.setValue(orig)
            return sb

        spin_left = make_focus_spin(left_focus_orig)
        spin_right = make_focus_spin(right_focus_orig)
        form.addRow("Left Side Focus:", spin_left)
        form.addRow("Right Side Focus:", spin_right)

        vbox.addLayout(form)

        # Reset-knapp för fokus
        reset_row = QHBoxLayout()
        btn_reset_focus = QPushButton("Reset focus to defaults (56/56)")
        def do_reset_focus():
            spin_left.setValue(DEF_FOCUS)
            spin_right.setValue(DEF_FOCUS)
        btn_reset_focus.clicked.connect(do_reset_focus)
        reset_row.addWidget(btn_reset_focus)
        reset_row.addStretch(1)
        vbox.addLayout(reset_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        vbox.addWidget(buttons)

        if dlg.exec() != QDialog.Accepted:
            return

        # --- Spara tillbaka, bara rader 9–16 justeras ---
        if len(top16) < 16:
            top16 += ["." for _ in range(16 - len(top16))]

        top16[IDX_KEEP_WORK]   = "y" if chk_keep_work.isChecked()  else "n"
        top16[IDX_SBS]         = "y" if chk_sbs.isChecked()        else "n"
        top16[IDX_KEEP_RIGHT]  = "y" if chk_keep_right.isChecked() else "n"
        top16[IDX_ANA]         = "y" if chk_ana.isChecked()        else "n"
        top16[IDX_SINGLE]      = "y" if chk_single.isChecked()     else "n"
        top16[IDX_PNG_MODE]    = "1" if chk_png.isChecked()        else "0"
        top16[IDX_LEFT_FOCUS]  = str(spin_left.value())
        top16[IDX_RIGHT_FOCUS] = str(spin_right.value())

        # Policy: när vi väl sparar vill vi inte ha helt tomma strängar i 9–16.
        # Om någon av dem råkat bli "" → skriv "." istället (Pascalkoden tolkar . som "tom" där det behövs).
        for idx in range(IDX_KEEP_WORK, IDX_RIGHT_FOCUS + 1):
            if top16[idx] == "":
                top16[idx] = "."

        new_lines = top16 + tail_lines
        path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

        if hasattr(self, "info_label"):
            self.info_label.setText(f"Saved {path.name}")
        print(f"[DEFAULTS] Saved {path}")

    # 0.9-079: ditching home3d_photos
    def export_selection(self, user_tag: str = ""):
        """
        Ny E/T-export:

        - Ingen home3d_photos, inga PNG:ar.
        - Bygger en VR_...bat med:
            * VR_META-rad (video, left/right index, dir_px, res_px)
            * ffmpeg-rad som skapar 360TB (v3d)
            * ffmpeg-rad som skapar LR (vlr)
        - Återanvänder:
            * _make_video_tag / _sanitize_user_tag
            * Insta360 vs valfri videologik
            * EXPORT_FRAME_PICK_MODE (select_by_index / timestamp_center)
            * Klickad horisontell riktning från overlay.
        """
        # Grundkoll: har vi S/D + video?
        if not hasattr(self, "left_frame") or not hasattr(self, "right_frame"):
            print("⚠ Please mark both left and right frames first.")
            return
        if self.left_frame is None or self.right_frame is None:
            print("⚠ Please mark both left and right frames first.")
            return
        if not self.video_path:
            print("⚠ No video_path set.")
            return

        left_idx = int(self.left_frame)
        right_idx = int(self.right_frame)

        # ---- Tagg-bygge (som tidigare i export_selection) ----
        tag, is_insta = self._make_video_tag(self.video_path)
        tag_prefix = f"{tag}_"

        user_tag_clean = self._sanitize_user_tag(user_tag)
        combined_tag_prefix = tag_prefix + user_tag_clean
        base_tag_for_scripts = combined_tag_prefix.rstrip("_")
        # Ex: 260205_167  eller  260205_167_Table-1

        # Suffix med framenummer (5 siffror som resten av systemet)
        source_suffix = f"__{left_idx:05d}_{right_idx:05d}"
        # Gemensam "bas" för outputnamn + scriptnamn
        base_core = f"{base_tag_for_scripts}{source_suffix}"
        # Exempel: 260205_167__00037_00054

        # ---- Video-ingång: Insta360 i app-rot, annars full sökväg ----
        video_basename = os.path.basename(self.video_path)
        if is_insta:
            video_in_root = runtime_path(video_basename)
            if not video_in_root.exists():
                msg = (
                    "Insta360 video file must be placed in the app folder.\n\n"
                    f"Expected:\n  {video_in_root}\n\n"
                    "Copy/move your VID_....mp4/.mov there and try again."
                )
                print("[EXPORT]", msg.replace("\n", " "))
                if hasattr(self, "info_label"):
                    self.info_label.setText("❌ Insta360 video not found in app folder (see console).")
                try:
                    from PySide6.QtWidgets import QMessageBox
                    QMessageBox.warning(self, "Video not in app folder", msg)
                except Exception:
                    pass
                return
            video_rel = video_basename
        else:
            # godtycklig 360-video – behåll full sökväg (med citattecken i kommandot)
            video_rel = self.video_path

        video_quoted = f"\"{video_rel}\""

        # ---- Källupplösning (bredd i pixlar) ----
        source_res = int(self.detect_source_width(self.video_path))
        if source_res <= 0:
            source_res = 7680
        if getattr(self, "is_insv_source", False): #0.9-087
            source_res *= 2 

        src_w = int(source_res)
        src_h = int(source_res // 2)  # typ 3840 vid 7680x3840

        # ---- Hämta klickad riktning och skala till källbredd ----
        overlay = getattr(self.video_view, "overlay", None)
        direction_px_drawn = getattr(overlay, "line_x", None) if overlay is not None else None
        if direction_px_drawn is None:
            msg = "⚠ Set direction: click in the video to choose horizontal direction before exporting."
            print(msg)
            if hasattr(self, "info_label"):
                self.info_label.setText(msg)
            return

        video_rect = self.video_view.video_item.boundingRect()
        drawn_w = float(video_rect.width()) if video_rect.isValid() else float(self.video_view.viewport().width() or 1.0)
        ratio = float(direction_px_drawn) / max(1.0, drawn_w)

        # horisontell position i källbilden (0..source_res-1)
        horiz_px = int(round(ratio * source_res))
        horiz_px = max(0, min(source_res - 1, horiz_px))

        # --- Riktning i pixlar, inte grader (facit: home3d_photos) ---
        #   right2left = (half - horiz_px) mod width
        #   left2right = width - right2left
        half_w = source_res // 2
        right2left = (half_w - horiz_px) % source_res
        left2right = (source_res - right2left) % source_res

        # Vi delar panoramat i en "stor" och en "liten" bit runt skärpunkten
        big_w = source_res - left2right   # t.ex. 7680 - 2247 = 5433
        small_w = left2right              # t.ex. 2247
        big_x = left2right                # startkolumn för "stora" biten
        small_x = 0                       # lilla biten börjar i x=0

        # Vertikal geometri
        h_half = source_res // 2          # 3840 vid 7680x3840
        quarter = source_res // 4         # 1920 vid 7680x3840
        three_quarter = src_w - quarter   # t.ex. 5760

        print(
            f"[EXPORT VR] horiz_px={horiz_px}  half={half_w}  → "
            f"right2left={right2left}, left2right={left2right}, "
            f"big_w={big_w}, small_w={small_w}, big_x={big_x}"
        )

        # ---- Output-filer (relativt root) ----
        out_tb_rel = f"{base_core}__360TB.jpg"   # 3D-360 TB (v3d)
        out_lr_rel = f"{base_core}_LR.jpg"       # LR-bild (framåtriktad)
        # RL hoppar vi över tills vidare – Skybox kan swappa LR<->RL.

        # ---- Scriptnamn: VR_...bat (gemensam kategori) ----
        script_name = f"VR_{base_core}.bat"
        queue_path = runtime_path(script_name)

        ff = get_ffmpeg_cmd()

        # ---- EXPORT_FRAME_PICK_MODE: välj hur vi plockar frames (vi använder nu alltid select_by_index och aldrig längre "by time".)----
        if EXPORT_FRAME_PICK_MODE == "select_by_index":
 
            ff = get_ffmpeg_cmd()
            is_insv = getattr(self, "is_insv_source", False)

            # ---- EXPORT_FRAME_PICK_MODE: välj hur vi plockar frames ----
            if EXPORT_FRAME_PICK_MODE != "select_by_index":
                raise ValueError(f"Unknown EXPORT_FRAME_PICK_MODE={EXPORT_FRAME_PICK_MODE!r}")

            if not is_insv:
                # Vanlig equirect-video: exakt frame via select=eq(n,...)
                # Not: Onödigt med två ggr -i. optimerat här för equirect med endast 0:v.

                input_args_tb = f'-i {video_quoted}'
                select_left_expr = f"select=eq(n\\,{left_idx}),format=rgb24"
                select_right_expr = f"select=eq(n\\,{right_idx}),format=rgb24"
            else:
                # Rå .insv med två videoströmmar 0:v:0 och 0:v:1. 
                # Not: Onödigt med två ggr -i. optimerat här för insv med endast 0:v.
                # Vi bygger två equirect-frames (vänster/höger) via:
                #   [X:v:0]select=... -> fXa
                #   [X:v:1]select=... -> fXb
                #   [fXa][fXb]hstack -> dfX
                #   dfX -> v360 dfisheye:e -> equirect (srcX)
                input_args_tb = f'-i {video_quoted}' # 0.9-087 tidigare dubbla -i, nu en enda 
                # format=rgb24 enbart före v360
                select_left_core = f"select=eq(n\\,{left_idx}),format=rgb24"
                select_right_core = f"select=eq(n\\,{right_idx}),format=rgb24"


        else:
            raise ValueError(f"Unknown EXPORT_FRAME_PICK_MODE={EXPORT_FRAME_PICK_MODE!r}")

        # LR använder samma input-argument som TB
        input_args_lr = input_args_tb


        tb_crop = (
                    # dela panoramat i 'stor' och 'liten' bit runt skärpunkten
                    f"[v0L]crop={big_w}:{h_half}:{big_x}:0[lL];"
                    f"[v0R]crop={small_w}:{h_half}:{small_x}:0[lR];"
                    f"[v1L]crop={big_w}:{h_half}:{big_x}:0[rL];"
                    f"[v1R]crop={small_w}:{h_half}:{small_x}:0[rR];"

                    # riktningskorrigerade TB-bilder per öga
                    f"[lL][lR]hstack,split=2[lDir1][lDir2];"
                    f"[rL][rR]hstack,split=2[rDir1][rDir2];"

                    # framåtriktad LeftTB
                    f"[lDir1]crop={h_half}:{h_half}:0:0[LT_top];"
                    f"[rDir1]crop={h_half}:{h_half}:0:0[LT_bot];"
                    f"[LT_top][LT_bot]vstack[LeftTB];"
    
                    # framåtriktad RightTB
                    f"[rDir2]crop={h_half}:{h_half}:{h_half}:0[RT_top];"
                    f"[lDir2]crop={h_half}:{h_half}:{h_half}:0[RT_bot];"
                    f"[RT_top][RT_bot]vstack[RightTB];"

                    # 'rotera' så att klickriktning hamnar i mitten
                    f"[LeftTB][RightTB]hstack,split=2[Forward1][Forward2];"
                    f"[Forward1]crop={quarter}:{source_res}:{3*quarter}:0[left_rot];"
                    f"[Forward2]crop={3*quarter}:{source_res}:0:0[right_rot];"
                    f"[left_rot][right_rot]hstack[v3d]"
        )

        if not is_insv:
            # --- 1) 360TB – klassisk pixelbaserad pipeline (v3d) för equirect-video ---
            filter_complex_tb = (
                # plocka ut vänster/höger-frame
                f"[0:v]{select_left_expr},split=2[v0L][v0R];"
                f"[0:v]{select_right_expr},split=2[v1L][v1R];"

                f"{tb_crop}"
            )
        else:
            # --- 1) 360TB – rå .insv: två fisheye-strömmar -> v360 dfisheye:e -> equirect ---
            filter_complex_tb = (
                # Vänster frame från första insv-input (index left_idx)
                f"[0:v:0]{select_left_core}[f0a];"
                f"[0:v:1]{select_left_core}[f0b];"
                f"[f0a][f0b]hstack=inputs=2[dfL];"
                f"[dfL]v360=dfisheye:e:"
                f"ih_fov=198:iv_fov=198:"
                f"yaw=0:pitch=0:roll=0,"
                f"split=2[v0L][v0R];"

                # Höger frame från samma insv-input (index right_idx)
                f"[0:v:0]{select_right_core}[g0a];"
                f"[0:v:1]{select_right_core}[g0b];"
                f"[g0a][g0b]hstack=inputs=2[dfR];"
                f"[dfR]v360=dfisheye:e:"
                f"ih_fov=198:iv_fov=198:"
                f"yaw=0:pitch=0:roll=0,"
                f"split=2[v1L][v1R];"

                # Därefter samma geometri som för equirect-fallet

                f"{tb_crop}"
            )

        cmd_line_tb = (
            f'{ff} -hide_banner -y '
            f'{input_args_tb} '
            f'-filter_complex "{filter_complex_tb}" '
            f'-map "[v3d]" -frames:v 1 -update 1 -q:v 1 "{out_tb_rel}"'
        )

        # --- 2) LR – separat ffmpeg-rad (vlr) ---
        lr_crop = (
            # dela panoramat i 'stor' och 'liten' bit runt skärpunkten
            f"[v0L]crop={big_w}:{h_half}:{big_x}:0[lL];" 
            f"[v0R]crop={small_w}:{h_half}:{small_x}:0[lR];"
            f"[v1L]crop={big_w}:{h_half}:{big_x}:0[rL];"
            f"[v1R]crop={small_w}:{h_half}:{small_x}:0[rR];"

            f"[lL][lR]hstack[lDirL];"
            f"[rL][rR]hstack[lDirR];"
  
            f"[lDirL][lDirR]vstack,split=2[lDir1][lDir2];"

            # Roterad LR (vlr) från singleL_forward
            f"[lDir1]crop={quarter}:{2*h_half}:{three_quarter}:0[L_left_rot];"
            f"[lDir2]crop={three_quarter}:{2*h_half}:0:0[L_right_rot];"
            f"[L_left_rot][L_right_rot]hstack[vlr];"
        )

        if not is_insv:
            filter_complex_lr = (
                f"[0:v]{select_left_expr},split=2[v0L][v0R];"
                f"[0:v]{select_right_expr},split=2[v1L][v1R];"

                f"{lr_crop}"
            )

        else:
            filter_complex_lr = (
                # Vänster frame från första insv-input (index left_idx)
                f"[0:v:0]{select_left_core}[f0a];"
                f"[0:v:1]{select_left_core}[f0b];"
                f"[f0a][f0b]hstack=inputs=2[dfL];"
                f"[dfL]v360=dfisheye:e:"
                f"ih_fov=198:iv_fov=198:"
                f"yaw=0:pitch=0:roll=0,"
                f"split=2[v0L][v0R];"

                # Höger frame från andra insv-input (index right_idx)
                # 0.9-087: ändrat till att hämtar från samma igen, 1:v -> 0:v
                f"[0:v:0]{select_right_core}[g0a];"
                f"[0:v:1]{select_right_core}[g0b];"
                f"[g0a][g0b]hstack=inputs=2[dfR];"
                f"[dfR]v360=dfisheye:e:"
                f"ih_fov=198:iv_fov=198:"
                f"yaw=0:pitch=0:roll=0,"
                f"split=2[v1L][v1R];"

                f"{lr_crop}"
            )


        cmd_line_lr = (
            f'{ff} -hide_banner -y '
            f'{input_args_lr} '
            f'-filter_complex "{filter_complex_lr}" '
            f'-map "[vlr]" -frames:v 1 -update 1 -q:v 1 "{out_lr_rel}"'
        )

        # --- Bygg VR_META-rad (för framtida "Edit from VR") ---
        meta_core = (
            f'VR_META '
            f'video="{video_basename}" '
            f'left={left_idx} right={right_idx} '
            f'dir_px={horiz_px} res_px={src_w}'
        )

        # --- Bygg scriptet rad för rad, cross-platform via IS_WIN ---
        script_lines = []

        if IS_WIN:
            # Windows / .bat
            script_lines.append("@echo off")
            script_lines.append("rem " + meta_core)
        else:
            # Linux/macOS / bash
            script_lines.append("#!/usr/bin/env bash")
            script_lines.append("set -euo pipefail")
            script_lines.append("# " + meta_core)

        script_lines.append(cmd_line_tb)
        script_lines.append(cmd_line_lr)

        try:
            with open(queue_path, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(script_lines) + "\n")

            if not IS_WIN:
                st = os.stat(queue_path)
                os.chmod(queue_path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

            print(f"[EXPORT VR] Script: {queue_path.name}  mode={EXPORT_FRAME_PICK_MODE}  "
                  f"left_idx={left_idx} right_idx={right_idx} fps={self.fps}")
            if hasattr(self, "info_label"):
                self.info_label.setText(f"VR script: {queue_path.name}")

            # Semantik som tidigare: nuvarande video har "exporterats"
            self._export_done_for_current_video = True

            # 0.9-084: bumpa intern vägmätare (Used)
            try:
                self._used_counter = int(getattr(self, "_used_counter", 0) or 0) + 1
            except Exception:
                self._used_counter = 1

            # Försök spara – men appen ska funka även om skrivning misslyckas
            try:
                self._save_settings()
            except Exception as e:
                print(f"[USED] Failed to save used_counter: {e}")


        except Exception as e:
            print(f"[EXPORT VR] Failed to write {queue_path}: {e}")
            if hasattr(self, "info_label"):
                self.info_label.setText(f"❌ Failed to create VR script: {e}")

    # -----------------------------
    # Open dialog
    # ------------------ 0.9-013 2) / 0.9-067
    def open_video_dialog(self):
        """Open a file dialog, with focus in the file list for quick keyboard use."""
        from pathlib import Path

        # 1) Startdir: senast använda om vi har en, annars mappen för aktuell video, annars Movies/home
        if getattr(self, "_settings_last_dir", None) and os.path.isdir(self._settings_last_dir):
            start_dir = self._settings_last_dir
        elif self.video_path and os.path.isdir(os.path.dirname(self.video_path)):
            start_dir = os.path.dirname(self.video_path)
        else:
            movies_dir = QStandardPaths.writableLocation(QStandardPaths.MoviesLocation)
            if movies_dir and os.path.isdir(movies_dir):
                start_dir = movies_dir
            else:
                start_dir = str(Path.home())

        # 2) Qt:s egen fil-dialog (ingen native Win-dialog → stabil i Wine)
        dlg = QFileDialog(self, "Open Video", start_dir)
        dlg.setFileMode(QFileDialog.ExistingFile)
        dlg.setNameFilter("Video Files (*.mp4 *.mov *.avi *.mkv *.insv)")
        dlg.setOption(QFileDialog.DontUseNativeDialog, True)
        dlg.setViewMode(QFileDialog.Detail)

        # 3) När dialogen väl är uppe, sätt fokus till fillistan
        def _focus_file_view():
            try:
                views = dlg.findChildren(QListView)
                if not views:
                    views = dlg.findChildren(QTreeView)
                if views:
                    views[0].setFocus()
            except Exception:
                pass

        # Schemalägg fokusbyte till "nästa varv i event-loopen"
        QTimer.singleShot(0, _focus_file_view)

        # 4) Kör dialogen
        if dlg.exec() != QFileDialog.Accepted:
            return

        files = dlg.selectedFiles()
        if not files:
            return

        file_path = files[0]
        self.load_video(file_path)
        self._settings_last_dir = os.path.dirname(file_path)
        self._save_settings()

    #0.9-087
    def _show_insv_roundtour_warning(self):
        """
        Diskret informationsruta för .insv-läget ("round tour raw").
        Stängs med OK, Enter, Esc eller mellanslag.
        Har checkbox för att stänga av framtida varningar.
        """
        dlg = QDialog(self)
        dlg.setWindowTitle("round tour raw")

        layout = QVBoxLayout(dlg)

        label = QLabel(
            "<b><span style='color:#555555;'>warning: loading INSV video</span></b><br><br>"

             "Now opening an insta 360 camera file directly. Metadata for gyros etc exists in .insv but I yet have to figure out some method for how to allign with the horizon. With the standard procedure Insta360 Studio -> Export to 360 this is a non-issue, Studio handles this nicely.<br>"
            "This feature might still be useful for special cases like an indoor guided tour. The hardest part seems to be moving the camera without changing its compass direction (yaw rotation) too much."
        ) 
        label.setWordWrap(True)
        layout.addWidget(label)

        # 0.9-087: möjlighet att stänga av
        checkbox = QCheckBox("don't show again")
        layout.addWidget(checkbox)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        layout.addWidget(buttons)

        def accept_and_maybe_disable():
            if checkbox.isChecked():
                self._show_insv_warning_flag = False
                # spara direkt så det gäller nästa gång
                try:
                    self._save_settings()
                except Exception as e:
                    print(f"[INSV WARNING] Failed to save settings: {e}")
            dlg.accept()

        buttons.accepted.connect(accept_and_maybe_disable)

        # Tillåt mellanslag för att stänga
        try:
            sc_space = QShortcut(Qt.Key_Space, dlg)
            sc_space.activated.connect(accept_and_maybe_disable)
        except Exception:
            pass

        dlg.resize(560, 220)  # lite bredare än tidigare (~+30%)
        dlg.exec()

    #0.9-007
    def handle_media_status(self, status):
        from PySide6.QtMultimedia import QMediaPlayer
        if status == QMediaPlayer.EndOfMedia:
            # 1) Pausa
            self.player.pause()
            self.paused = True
     
            # 2) Räkna ut sista giltiga frame-index
            duration_ms = int(self.player.duration())
            last_idx = max(0, self._pos_to_frame(max(0, duration_ms - 1)))

            # 3) Sätt exakt center-tid för sista rutan (undvik gränsfladder)
            last_pos = self._frame_to_pos(last_idx)
            self._snap_guard = True
            try:
                self.player.setPosition(last_pos)
            finally:
                self._snap_guard = False

            # 4) Håll auktoritativt state synkat
            self.current_frame = last_idx
            print(f"Reached end — paused on last frame: {last_idx} (pos={last_pos} ms)")
    #0.89-006 step 2
    def _sanitize_user_tag(self, s: str) -> str:
        """Return a filesystem-friendly tag, with trailing '_' if non-empty."""
        if not s:
            return ""
        s = s.strip().replace(" ", "-")
        # keep only alnum, dash, underscore
        s = "".join(ch for ch in s if ch.isalnum() or ch in "-_")
        if not s:
            return ""
        if not s.endswith("_"):
            s += "_"
        return s

    #0.9-046 / 0.9-064
    def _bat_kind_and_base(self, path: Path):
        """
        Classify BAT/SH scripts into logical kinds:

          - "source"  = Film/Video jobs (extract L/R PNG from video)
          - "stereo"  = Photo jobs (home3d_photos, final images)

        Returns (kind, base) where base is the common suffix used to link
        Film/Photo jobs together, e.g. "251208_149_Table-1__00001_00023".
        """
        name = path.name
        # strip extension .bat/.sh
        if name.endswith(".bat"):
            core = name[:-4]
        elif name.endswith(".sh"):
            core = name[:-3]
        else:
            return None, None

        # Legacy names
        if core.startswith("Source_images_"):
            return "source", core[len("Source_images_"):]
        if core.startswith("Stereo_image_"):
            return "stereo", core[len("Stereo_image_"):]

        # New(er) names
        if core.startswith("Film_"):
            return "source", core[len("Film_"):]
        if core.startswith("Video_"):
            # äldre kortnamn – behandlas som Film_
            return "source", core[len("Video_"):]
        if core.startswith("Photo_"):
            return "stereo", core[len("Photo_"):]
        if core.startswith("VR_"):
            # Ny typ: direkt-ffmpeg-jobb som skapar 3D360 + LR/RL
            # Behandlas som "stereo" i all statuslogik.
            return "stereo", core[len("VR_"):]

        return None, None

    #0.9-020
    def _bat_status(self, path: Path, kind: str, base: str) -> str:
        """
        Slutlig status för ett script:
          - pending
          - in_progress
          - aborted
          - done
    
        kind: "source" eller "stereo"
        base: t.ex. "251124_149_Table-1__0002_0003"
        """
        root = RUNTIME_DIR

        # --- 1) Filesystem: är "nästa steg" klart? ---
        fs_done = False

        if kind == "source":
            # Source/Video script is "done" when a matching Photo/Stereo script exists.
            # base already includes the __0002_0003 part – exact pair matching.
            stereo_cores = [
                f"Stereo_image_{base}",  # legacy
                f"Photo_{base}",         # new
            ]
            hits = []
            for core in stereo_cores:
                for ext in (".bat", ".sh"):
                    hits.extend(root.glob(core + ext))
            fs_done = bool(hits)

        elif kind == "stereo":
            # Stereo_image_<base>.bat → done när slutbild finns:
            #   <base>__*360TB*.jpg (ev. png)
            img_hits = []
            for pattern in (f"{base}__*360TB*.jpg",
                            f"{base}__*360TB*.png"):
                img_hits.extend(root.glob(pattern))
            fs_done = bool(img_hits)

        # --- 2) Runtime: har VI startat ett terminalfönster för detta script? ---
        path_key = str(path.resolve())
        proc = self._bat_procs.get(path_key)

        if proc is not None:
            # Processen (terminalen) lever fortfarande?
            if proc.poll() is None:
                return "in_progress"

            # Processen är klar → ta bort handle
            del self._bat_procs[path_key]

            # Klar + output finns → done, annars aborted
            if fs_done:
                self._bat_manual_state.pop(path_key, None)
                return "done"
            else:
                self._bat_manual_state[path_key] = "aborted"
                return "aborted"

        # --- 3) Ingen aktiv process: kolla om vi tidigare markerat aborted ---
        if self._bat_manual_state.get(path_key) == "aborted":
            # Om output senare dyker upp (manuell körning), uppgradera till done
            if fs_done:
                self._bat_manual_state.pop(path_key, None)
                return "done"
            return "aborted"

        # --- 4) Annars bara filesystem: done/pending ---
        return "done" if fs_done else "pending"

    # 0.9-063
    def _compute_blupp(self, root: Path, p: Path, kind: str, base: str, mtime: float) -> bool:
        """
        'Blupp' = markering för Arne att scriptet är omgjort och behöver köras om.

        Video/source (.bat med "Video_..."):
          blupp = video.bat är NYARE än sin photo/stereo-bat

        Photo/stereo (.bat med "Photo_..." eller "Stereo_image_..."):
          blupp = photo.bat är NYARE än sin 3D360-bild (...360TB...)

        Färgerna (pending/done/in_progress/aborted) styrs som tidigare av _bat_status.
        """
        try:
            if kind == "source":
                # Video_... -> hitta matchande stereo/photo-script med samma base
                stereo_cores = [
                    f"Stereo_image_{base}",
                    f"Photo_{base}",
                ]
                stereo_paths = []
                for core in stereo_cores:
                    stereo_paths.extend(root.glob(core + ".bat"))
                if not stereo_paths:
                    # Inget photo-script ännu -> inget att "omgöra"
                    return False

                newest_stereo_mtime = max(
                    (sp.stat().st_mtime for sp in stereo_paths),
                    default=0.0,
                )
                # Blupp om video.bat är nyare än sitt stereo-script
                return mtime > (newest_stereo_mtime + 0.5)

            elif kind == "stereo":
                # Photo_.../Stereo_image_... -> hitta 3D360-bild(er) för samma base
                img_hits = []
                for pattern in (f"{base}__*360TB*.jpg",
                                f"{base}__*360TB*.png"):
                    img_hits.extend(root.glob(pattern))

                if not img_hits:
                    # Inga 360TB-bilder ännu -> standard pending/röd får räcka
                    return False

                newest_img_mtime = max(
                    (ip.stat().st_mtime for ip in img_hits),
                    default=0.0,
                )
                # Blupp om photo.bat är nyare än sin senaste 360TB-bild
                return mtime > (newest_img_mtime + 0.5)

        except Exception as e:
            print(f"[BLUPP] compute failed for {p}: {e}")

        return False


    #0.9-020 / 0.9-064
    def _scan_bat_files(self):
        """Skanna appens root (RUNTIME_DIR) efter kända .bat-skript."""
        root = RUNTIME_DIR
        patterns = ["*.bat"]  # även på Linux; .sh är bara interna wrappers

        paths = []
        for pat in patterns:
            paths.extend(root.glob(pat))

        entries = []
        for p in paths:
            kind, base = self._bat_kind_and_base(p)
            if kind is None:
                continue

            try:
                st = p.stat()
                mtime = float(st.st_mtime)
            except Exception:
                mtime = 0.0

            status = self._bat_status(p, kind, base)
            blupp = self._compute_blupp(root, p, kind, base, mtime)

            entries.append({
                "path": p,
                "kind": kind,       # "source" (Film/Video) eller "stereo" (Photo)
                "base": base,
                "status": status,   # pending / done / in_progress / aborted
                "blupp": blupp,     # ⟳-flagga enligt vår logik
            })

        # Sortera:
        #   1) source (Film/Video/Source_images) överst
        #   2) stereo (Photo/Stereo_image) sist
        #   och inom respektive grupp: alfabetiskt på filnamn (YYMMDD... ger datumordning)
        def sort_key(e):
            kind_order = 0 if e["kind"] == "source" else 1
            return (kind_order, e["path"].name.lower())

        entries.sort(key=sort_key)
        return entries

    #0.9-035 / 0.9-063
    def _refresh_bat_list(self):
        # 1) Kom ihåg vilka paths som var valda innan vi nollställer listan
        selected_paths = set()
        for it in self.bat_list.selectedItems():
            path_str = it.data(Qt.UserRole)
            if path_str:
                selected_paths.add(path_str)

        self.bat_list.clear()

        reserved_sources = 0  # source .bat i in_progress
        entries = self._scan_bat_files()

        # 2) Bygg listan på nytt
        for entry in entries:
            p = entry["path"]
            status = entry["status"]
            kind = entry["kind"]          # "source" (Video) eller "stereo" (Photo)
            blupp = entry.get("blupp", False)

            # Räkna "reserverade" krediter: source/Video + in_progress
            if kind == "source" and status == "in_progress":
                reserved_sources += 1

            # Status-text + liten "blupp" för om-export:
            #
            #  - vi rör inte grundstaten (pending/done/...), utan lägger bara ⟳ efter texten
            status_text = status
            if blupp:
                status_text = f"{status} ⟳"

            # Kompakt label: filnamn + status
            label = f"{p.name}   {status_text}"

            it = QListWidgetItem(label)

            # Färgkodning per huvudsstate:
            #   done          → grön
            #   in_progress   → blå
            #   aborted       → röd
            #   pending       → ljusbrun/amber
            #   pending ⟳     → lite mer orange (men inte röd)
            if status == "done":
                it.setForeground(QColor("#2e7d32"))      # grön
            elif status == "in_progress":
                it.setForeground(QColor("#1565c0"))      # blå
            elif status == "aborted":
                it.setForeground(QColor("#c62828"))      # röd
            else:  # pending + ev. okända
                if blupp:
                    it.setForeground(QColor("#d47f00"))  # pending med blupp – lite mer orange
                else:
                    it.setForeground(QColor("#b26a00"))  # vanlig pending (ljusbrun)

            path_str = str(p)
            it.setData(Qt.UserRole, path_str)

            # 3) Återställ markering om samma path fanns vald tidigare
            if path_str in selected_paths:
                it.setSelected(True)

            self.bat_list.addItem(it)

        # 4) Uppdatera film-labelen efter att listan byggts
        self._update_credits_label(0) # 0.9-084 now 0 as the function is disabled 

    # 0.9-084 pascal counter removed, now simple counter
    def _update_credits_label(self, reserved_sources: int):
        """
        Enkel vägmätare: visar bara antal använda exponeringar (used_counter).

        reserved_sources ignoreras nu – vi har inget externt credits-system längre.
        """
        if not hasattr(self, "bat_credits_label"):
            return

        used = int(getattr(self, "_used_counter", 0) or 0)
        self.bat_credits_label.setText(f"done: {used}")

    #0.9-020
    def _run_bat(self, bat_path: str):
        try:
            p = Path(bat_path).resolve()
            path_key = str(p)  # vi använder alltid .bat som nyckel

            # Ny körning: rensa ev. gammal "aborted"-flagga
            self._bat_manual_state.pop(path_key, None)

            if IS_WIN:
                # Windows: ny synlig cmd i scriptets katalog, /k eller /c styrt av settings
                mode = getattr(self, "_settings_cmd_mode", "k")
                cmd_flag = "/k"
                if str(mode).lower().startswith("c"):
                    cmd_flag = "/c"

                proc = subprocess.Popen(
                    ["cmd.exe", cmd_flag, p.name],
                    cwd=str(p.parent),
                    creationflags=subprocess.CREATE_NEW_CONSOLE
                )
                self._bat_procs[path_key] = proc
                print(f"[RUN] {p.name} (Windows {cmd_flag}, cwd={p.parent})")
                return

            # ---- Linux / Mint ----
            import shutil
            def which(x): return shutil.which(x) is not None

            to_run = p
            name_low = p.name.lower()

            # Om stereo/photo .bat på Linux -> konvertera till .sh först
            if p.suffix.lower() == ".bat" and (
                name_low.startswith("stereo_image_") or name_low.startswith("photo_") # 0.9-046 photo.bat 
            ):
                to_run = self._bat_to_sh(p)

            run_cwd = str(to_run.parent)
            payload = f"./{to_run.name}"

            candidates = []
            if which("x-terminal-emulator"):
                candidates.append(["x-terminal-emulator", "-e", "bash", "-lc", payload])
            if which("gnome-terminal"):
                candidates.append(["gnome-terminal", "--", "bash", "-lc", payload])
            if which("konsole"):
                candidates.append(["konsole", "-e", "bash", "-lc", payload])
            if which("xfce4-terminal"):
                candidates.append(["xfce4-terminal", "-e", "bash", "-lc", payload])
            if which("xterm"):
                candidates.append(["xterm", "-e", "bash", "-lc", payload])

            proc = None
            for cmd in candidates:
                try:
                    proc = subprocess.Popen(cmd, cwd=run_cwd)
                    print(f"[RUN] {to_run.name} ({cmd[0]}, cwd={run_cwd})")
                    break
                except Exception as e:
                    print(f"[RUN] {cmd[0]} failed: {e}")

            if proc is None:
                # Sista fallback: bakgrunds-bash
                proc = subprocess.Popen(["bash", "-lc", payload], cwd=run_cwd)
                print(f"[RUN] {to_run.name} (background bash, cwd={run_cwd})")

            # Spara process-handle på .bat-nyckeln
            self._bat_procs[path_key] = proc

        except Exception as e:
            print(f"[RUN] Failed {bat_path}: {e}")

    #0.9-027
    def _bat_refresh_tick(self):
        try:
            # Om dockan inte är synlig – gör inget
            if not self.bat_dock.isVisible():
                return

            # Pausa när context-menyn är öppen
            if getattr(self, "_bat_context_open", False):
                return

            # Ta reda på om musen ligger över en faktisk rad i listan
            global_pos = QCursor.pos()
            local_pos = self.bat_list.viewport().mapFromGlobal(global_pos)
            item_under_mouse = self.bat_list.itemAt(local_pos)

            # Om musen är över en rad → pausa refresh (så urvalet inte störs)
            if item_under_mouse is not None:
                return

        except Exception:
            # Om något strular här vill vi inte döda timern
            pass

        # I övriga fall – uppdatera listan
        self._refresh_bat_list()

    #0.9-040
    def _run_selected_bat(self):
        items = self.bat_list.selectedItems()
        if not items:
            it = self.bat_list.currentItem()
            if not it:
                return
            items = [it]

        # Plocka ut paths från valda rader
        selected_paths = []
        for it in items:
            path = it.data(Qt.UserRole)
            if path:
                selected_paths.append(path)

        if not selected_paths:
            return

        # Ta reda på vilken sort (source/stereo) varje path är
        entries = self._scan_bat_files()
        kind_map = {str(e["path"]): e["kind"] for e in entries}

        source_paths = []
        stereo_paths = []

        for path in selected_paths:
            kind = kind_map.get(path)
            if kind == "source":
                source_paths.append(path)
            else:
                stereo_paths.append(path)

        # 1) SOURCE-bat – gå alltid via sekventiell kö
        if source_paths:
            if getattr(self, "_source_queue_busy", False):
                # Redan en kö igång → starta inte fler
                self._show_source_busy_hint()
            else:
                # Här kan du välja ordning – vi kör i samma ordning som användaren markerat
                # Vill du ha äldst först även här kan du göra:
                # source_paths = list(reversed(source_paths))
                print(f"[RUN] Startar {len(source_paths)} source-bat via sekventiell kö.")
                self._run_bats_sequentially(source_paths)

        # 2) STEREO-bat – körs som förut, direkt
        for path in stereo_paths:
            self._run_bat(path)

    # 0.9-036
    def _run_bats_with_delay(self, paths, delay_ms=250, index=0):
        """
        Kör bat-filerna i 'paths' en i taget, med delay_ms mellan varje start.
        paths: lista med str (filnamn)
        """
        if index >= len(paths):
            return

        path = paths[index]
        # Starta detta script
        self._run_bat(path)

        # Schemalägg nästa om det finns fler
        if index + 1 < len(paths):
            QTimer.singleShot(
                delay_ms,
                lambda: self._run_bats_with_delay(paths, delay_ms, index + 1)
            )

     # 0.9-034 / 0.9-043 /0.9-086
    def _archive_all(self):
        """
        Arkivera allt arbetsmaterial till en datumstämplad MAPP med platt struktur:

          archive_YYMMDD/
              *.mov, *.mp4, *.mkv          (videofiler)
              VR_*.bat                     (allt-i-ett VR-skript)
              *.jpg, *.jpeg, *.png         (färdiga bilder i root)
              (alla *.png i ./VideoOneshot/ RADERAS – gamla workfiles)

        Inget annat rörs. Övriga .bat, gamla Film_/Photo_/source-skript osv lämnas kvar.
        """

        root = RUNTIME_DIR

        today_tag = date.today().strftime("%y%m%d")
        archive_root = runtime_path(f"archive_{today_tag}")
        mono_dir = runtime_path(MONO_DIR_NAME)

        archive_root.mkdir(exist_ok=True)

        # Bekräfta
        try:
            from PySide6.QtWidgets import QMessageBox
            msg = (
                "Archive all?\n\n"
                f"• Move video files (*.mov/*.mp4/*.mkv) to ./archive_{today_tag}/\n"
                f"• Move VR scripts (VR_*.bat) to ./archive_{today_tag}/\n"
                f"• Move image files (*.jpg/*.jpeg/*.png in root) to ./archive_{today_tag}/\n"
                f"• Delete all *.png in ./{MONO_DIR_NAME}/ (old one-shot workfiles)\n"
                "\nNo other files are touched."
            )
            ans = QMessageBox.question(
                self,
                "Archive all",
                msg,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ans != QMessageBox.Yes:
                return
        except Exception:
            pass

        import shutil

        moved_video_files = 0
        moved_scripts = 0
        moved_photo_files = 0
        deleted_mono_png = 0

        # --- Flytta filer i root ---
        try:
            for p in root.iterdir():
                if not p.is_file():
                    continue

                suf = p.suffix.lower()
                name = p.name

                dest = None
                target_counter = None

                # Videofiler -> archive_root/
                if suf in (".mov", ".mp4", ".mkv"):
                    dest = archive_root / name
                    target_counter = "video_file"

                # VR-skript (.bat) -> archive_root/
                elif suf == ".bat" and name.startswith("VR_"):
                    dest = archive_root / name
                    target_counter = "script"

                # Färdiga bilder i root -> archive_root/
                elif suf in (".jpg", ".jpeg", ".png"):
                    dest = archive_root / name
                    target_counter = "photo_file"

                if dest is None:
                    continue

                try:
                    if dest.exists():
                        dest.unlink()
                    shutil.move(str(p), str(dest))

                    if target_counter == "video_file":
                        moved_video_files += 1
                    elif target_counter == "script":
                        moved_scripts += 1
                    elif target_counter == "photo_file":
                        moved_photo_files += 1

                except Exception as e:
                    print(f"[ARCHIVE] Failed to move {p}: {e}")
        except Exception as e:
            print(f"[ARCHIVE] Root scan failed: {e}")

        # --- Rensa mono-PNGs från arbetsmappen VideoOneshot/ ---
        try:
            if mono_dir.exists():
                for p in mono_dir.glob("*.png"):
                    if not p.is_file():
                        continue
                    try:
                        p.unlink()
                        deleted_mono_png += 1
                    except Exception as e:
                        print(f"[ARCHIVE] Failed to delete mono PNG {p}: {e}")

                # Om mappen är tom efteråt kan vi lugnt försöka ta bort den
                try:
                    if not any(mono_dir.iterdir()):
                        mono_dir.rmdir()
                except Exception:
                    pass
        except Exception as e:
            print(f"[ARCHIVE] Mono dir cleanup failed: {e}")

        msg = (
            f"Archived to archive_{today_tag}/\n"
            f"  video files: {moved_video_files}\n"
            f"  VR scripts (VR_*.bat): {moved_scripts}\n"
            f"  image files in root: {moved_photo_files}\n"
            f"  deleted {MONO_DIR_NAME}/*.png: {deleted_mono_png}"
        )
        print("[ARCHIVE]", msg)
        if hasattr(self, "info_label"):
            self.info_label.setText(msg)

        # Uppdatera BAT-listan efter flytten
        self._refresh_bat_list()

    #0.9-022, 0.9-081
    def _edit_selected_from_bat(self):
        """
        Ta vald script-rad och återställ:

          - Film/Video (source-skript) via home3d_photos-data
          - VR_...bat via VR_META

        Så att vi landar i 'redo att exportera'-läget igen.
        """
        it = self.bat_list.currentItem()
        if not it:
            items = self.bat_list.selectedItems()
            if not items:
                return
            it = items[0]

        path_str = it.data(Qt.UserRole)
        if not path_str:
            return

        p = Path(path_str)
        name = p.name
        kind, base = self._bat_kind_and_base(p)

        # Bekräfta (samma dialog för båda varianterna)
        try:
            from PySide6.QtWidgets import QMessageBox
            ans = QMessageBox.question(
                self,
                "Restore selection",
                f"Load video + stereo settings from\n{name}?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if ans != QMessageBox.Yes:
                return
        except Exception:
            pass

        # VR_...bat → läs VR_META
        if name.startswith("VR_"):
            self._edit_vr_script(path_str)
            return

        # Klassiska Film/Video-source-skript → befintlig logik
        if kind != "source":
            if hasattr(self, "info_label"):
                self.info_label.setText("Edit works only on Film/Video or VR scripts.")
            return

        self._edit_source_script(path_str)

    def _edit_source_script(self, bat_path_str: str):
        """Läs Source_images_*.bat och återställ video + L/R + riktning + stereo-parametrar."""
        p = Path(bat_path_str)
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            print(f"[EDIT] Failed to read {p}: {e}")
            if hasattr(self, "info_label"):
                self.info_label.setText(f"❌ Could not read {p.name}")
            return

        raw_lines = text.splitlines()
        # behåll rader men skippa rena tomrader för enklare scanning
        lines = [ln for ln in raw_lines if ln.strip()]

        # --- FFmpeg-raderna: första = left, andra = right ---
        ff_lines = [ln for ln in lines if "ffmpeg" in ln.lower()]
        if len(ff_lines) < 2:
            print(f"[EDIT] Not enough ffmpeg lines in {p.name}")
            if hasattr(self, "info_label"):
                self.info_label.setText(f"❌ Cannot parse {p.name} (ffmpeg lines)")
            return

        left_line = ff_lines[0]
        right_line = ff_lines[1]

        def _parse_ff_line(line: str):
            """Return (video_path, frame_idx) från ffmpeg-rad."""
            try:
                toks = shlex.split(line, posix=not IS_WIN)
            except Exception:
                toks = line.split()

            video_path = None
            frame_idx = None

            # hitta -i <path>
            for i, t in enumerate(toks):
                if t == "-i" and i + 1 < len(toks):
                    video_path = toks[i + 1]
                    break

            # hitta select=eq(n\,N) eller select=eq(n,N)
            m = re.search(r"select=eq\(n\\,(\d+)\)", line)
            if not m:
                m = re.search(r"select=eq\(n,(\d+)\)", line)
            if m:
                try:
                    frame_idx = int(m.group(1))
                except Exception:
                    frame_idx = None

            return video_path, frame_idx

        v1, left_frame = _parse_ff_line(left_line)
        v2, right_frame = _parse_ff_line(right_line)
        video_path = v1 or v2

        video_path = v1 or v2

        if not video_path:
            print(f"[EDIT] No video path found in ffmpeg lines for {p.name}")
            if hasattr(self, "info_label"):
                self.info_label.setText(f"❌ Cannot find video in {p.name}")
            return

        # Försök lösa upp sökvägen så robust vi kan 0.9-068
        video_path_fs = video_path

        try:
            if not os.path.isabs(video_path):
                candidate1 = (p.parent / video_path)
                candidate2 = runtime_path(video_path)

                print(f"[EDIT] video_path token: {video_path}")
                print(f"[EDIT] candidate1 (script dir): {candidate1} exists={candidate1.exists()}")
                print(f"[EDIT] candidate2 (runtime dir): {candidate2} exists={candidate2.exists()}")

                if candidate1.exists():
                    video_path_fs = str(candidate1)
                elif candidate2.exists():
                    video_path_fs = str(candidate2)
        except Exception as e:
            print(f"[EDIT] Exception while resolving video path for {p.name}: {e}")

        print(f"[EDIT] final video_path_fs: {video_path_fs} exists={os.path.exists(video_path_fs)}")

        # --- Hitta home3d_photos-raden (exe_line) ---
        exe_line = None
        for ln in reversed(lines):
            low = ln.lower()
            if "home3d_photos" in low:
                exe_line = ln
                break
        if exe_line is None:
            # fallback: sista icke-ffmpeg- och icke-kommentarrad
            for ln in reversed(lines):
                low = ln.strip().lower()
                if low.startswith("#") or low.startswith("@rem") or "ffmpeg" in low:
                    continue
                exe_line = ln
                break

        if exe_line is None:
            print(f"[EDIT] No home3d_photos line in {p.name}")
            if hasattr(self, "info_label"):
                self.info_label.setText(f"❌ Cannot find home3d_photos in {p.name}")
            return

        # --- Plocka ut de 16 parametrarna efter kommandot ---
        try:
            toks = shlex.split(exe_line, posix=not IS_WIN)
        except Exception:
            toks = exe_line.split()

        if len(toks) < 16 + 1:
            print(f"[EDIT] Too few tokens in exe_line for {p.name}: {toks}")
            if hasattr(self, "info_label"):
                self.info_label.setText(f"❌ Cannot parse params in {p.name}")
            return

        params = toks[-16:]  # sista 16 = våra 1..16

        # --- Plocka ut resolution + riktning (pixlar i källbredd) ---
        try:
            source_res = int(params[IDX_RES])
        except Exception:
            source_res = 7680

        try:
            horiz_dir = int(params[IDX_DIR])
        except Exception:
            horiz_dir = source_res // 2

        # --- Jämför stereo-inställningarna (rader 9–16) med defaults ---
        # 0.9-060
        defaults_path = runtime_path("home3d_photos_defaults.txt")
        try:
            all_lines = defaults_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            all_lines = []
        if len(all_lines) < 16:
            all_lines += ["." for _ in range(16 - len(all_lines))]
        top16 = all_lines[:16]

        # Stereo-parametrar 9..16: [keep_work, sbs, keep_right, ana, single, png_mode, left_focus, right_focus]
        script_stereo   = params[IDX_KEEP_WORK:]      # 9..16 från scriptet (i källbreddens pixlar)
        defaults_stereo = top16[IDX_KEEP_WORK:]       # 9..16 från defaults (referensbredd 7680)

        # Normalisera fokus-värdena i scriptet tillbaka till 7680-bredd
        script_norm = list(script_stereo)
        try:
            source_res = int(params[IDX_RES])
        except Exception:
            source_res = 7680

        if source_res > 0:
            idx_lf_rel = IDX_LEFT_FOCUS  - IDX_KEEP_WORK  # 14 - 8 = 6
            idx_rf_rel = IDX_RIGHT_FOCUS - IDX_KEEP_WORK  # 15 - 8 = 7
            try:
                lf_px = int(script_norm[idx_lf_rel])
                rf_px = int(script_norm[idx_rf_rel])
                scale_back = 7680.0 / float(source_res)
                lf_base = int(round(lf_px * scale_back))
                rf_base = int(round(rf_px * scale_back))
                script_norm[idx_lf_rel] = str(lf_base)
                script_norm[idx_rf_rel] = str(rf_base)
            except Exception:
                pass

        stereo_diff = (script_norm != defaults_stereo)

        # --- Ladda video + återställ L/R + riktning --- 0.9-045 C
        self.load_video(video_path_fs)

        if left_frame is not None:
            self.left_frame = int(left_frame)
            self._jump_to_frame(int(left_frame))
        if right_frame is not None:
            self.right_frame = int(right_frame)

        # Riktning (översätt käll-pixel → overlay-X i vy)
        try:
            video_rect = self.video_view.video_item.boundingRect()
            drawn_w = float(video_rect.width()) if video_rect.isValid() else float(self.video_view.viewport().width() or 1.0)
            if drawn_w > 0 and source_res > 0:
                ratio = float(horiz_dir) / float(source_res)
                x = max(0.0, min(drawn_w, ratio * drawn_w))
                self.video_view.line_x = x
                if hasattr(self.video_view, "overlay") and self.video_view.overlay is not None:
                    self.video_view.overlay.set_line_x(x)
                    self.video_view.overlay.update()
        except Exception as e:
            print(f"[EDIT] Direction restore failed: {e}")

        # Info-rad
        if hasattr(self, "info_label"):
            self.info_label.setText(
                f"Restored from {p.name} | L={left_frame} R={right_frame} dir={horiz_dir}/{source_res}"
            )

        # --- Om stereo-inställningarna skiljer sig (efter normalisering), erbjud att synca defaults ---
        if stereo_diff:
            script_str   = " ".join(script_norm)
            defaults_str = " ".join(defaults_stereo)
            msg = (
                f"Stereo settings differ for {p.name}.\n\n"
                f"From image script (normalized to 7680):  {script_str}\n"
                f"Current defaults:                        {defaults_str}\n\n"
                "Update defaults to match the image script?"
            )
            try:
                from PySide6.QtWidgets import QMessageBox
                ans = QMessageBox.question(
                    self,
                    "Stereo settings differ",
                    msg,
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if ans == QMessageBox.Yes:
                    # Spara den normaliserade varianten (7680-baserad)
                    top16[IDX_KEEP_WORK:] = script_norm
                    new_lines = top16 + all_lines[16:]
                    defaults_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                    if hasattr(self, "info_label"):
                        self.info_label.setText(f"Updated defaults from {p.name}")
            except Exception as e:
                print(f"[EDIT] Stereo-diff prompt failed: {e}")
#    # före 0.9-036
#    def _run_all_pending(self):
#        # Launch all pending (both kinds) in reverse time order (newest first)
#        entries = self._scan_bat_files()
#        pendings = [e for e in entries if e["status"] == "pending"]
#        for e in pendings:
#            self._run_bat(str(e["path"]))
#        # quick refresh to show current state; full completion will reflect on next refresh
#        QTimer.singleShot(500, self._refresh_bat_list)


    # 0.9-081: plocka ut VR_META ur VR_...bat
    def _extract_vr_meta_from_script(self, p: Path):
        """
        Läser ett VR_*.bat (eller motsv. .sh) och plockar ut VR_META:

          VR_META video="<filnamn>" left=N right=M dir_px=D res_px=R

        Returnerar dict:
          {
            "video": <str>,
            "left": <int>,
            "right": <int>,
            "dir_px": <int>,
            "res_px": <int>,
          }
        eller None vid fel.
        """
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            print(f"[EDIT-VR] Failed to read {p}: {e}")
            if hasattr(self, "info_label"):
                self.info_label.setText(f"❌ Could not read {p.name}")
            return None

        lines = text.splitlines()

        meta_line = None
        for ln in lines:
            if "VR_META" in ln:
                meta_line = ln
                break

        if meta_line is None:
            print(f"[EDIT-VR] No VR_META line found in {p.name}")
            if hasattr(self, "info_label"):
                self.info_label.setText(f"❌ No VR_META in {p.name}")
            return None

        # Plocka ut biten från och med 'VR_META ...'
        idx = meta_line.find("VR_META")
        meta = meta_line[idx:]

        def _int_field(pattern, default=None):
            m = re.search(pattern, meta)
            if not m:
                return default
            try:
                return int(m.group(1))
            except Exception:
                return default

        m_video = re.search(r'video="([^"]+)"', meta)
        video_token = m_video.group(1) if m_video else None

        left_idx  = _int_field(r'\bleft=(\d+)\b')
        right_idx = _int_field(r'\bright=(\d+)\b')
        dir_px    = _int_field(r'\bdir_px=(\d+)\b')
        res_px    = _int_field(r'\bres_px=(\d+)\b')

        if not video_token or left_idx is None or right_idx is None:
            print(f"[EDIT-VR] Incomplete VR_META in {p.name}: {meta}")
            if hasattr(self, "info_label"):
                self.info_label.setText(f"❌ Incomplete VR_META in {p.name}")
            return None

        if res_px is None or res_px <= 0:
            res_px = 7680  # rimlig fallback

        return {
            "video": video_token,
            "left": int(left_idx),
            "right": int(right_idx),
            "dir_px": int(dir_px) if dir_px is not None else res_px // 2,
            "res_px": int(res_px),
        }


    def _run_all_pending(self):
        # Hämta alla pending (både source och stereo), newest first i listan
        entries = self._scan_bat_files()
        pendings = [e for e in entries if e["status"] == "pending"]

        # Dela upp i source/stereo
        source_paths = []
        stereo_paths = []

        for e in pendings:
            p = str(e["path"])
            if e["kind"] == "source":
                source_paths.append(p)
            else:
                stereo_paths.append(p)

        # Kör äldst först i respektive kategori
        source_paths = list(reversed(source_paths))
        stereo_paths = list(reversed(stereo_paths))

        if not source_paths and not stereo_paths:
            print("[RUN] Inga pending .bat att köra.")
            return

        # 1) SOURCE-bat sekventiellt – men bara om vi inte redan har en kö igång
        if source_paths:
            if getattr(self, "_source_queue_busy", False):
                # Redan igång → starta inte en ny sekvens, bara informera lite
                self._show_source_busy_hint()
            else:
                print(
                    f"[RUN] Kör {len(source_paths)} SOURCE .bat sekventiellt (äldst först)."
                )
                self._run_bats_sequentially(source_paths)

        # 2) STEREO-bat: parallellt, men startade med lite paus emellan
        if stereo_paths:
            stereo_delay_ms = 600  # din nuvarande kompromiss
            print(
                f"[RUN] Kör {len(stereo_paths)} STEREO .bat (äldst först) med "
                f"{stereo_delay_ms} ms mellan start (parallellt efterhand)."
            )
            self._run_bats_with_delay(stereo_paths, delay_ms=stereo_delay_ms)

        # Liten extra-refresh efter en stund (UI:n sköter resten via _bat_refresh_tick)
        QTimer.singleShot(500, self._refresh_bat_list)

    #0.9-039
    def _run_bats_sequentially(self, paths, index=0):
        """
        Kör bat-filerna i 'paths' en i taget.
        paths: lista med str (fulla bat-sökvägar)
        Används för SOURCE-bat som pratar med home3d_photos/credits.
        """
        from pathlib import Path

        # Första anropet: markera att vi är inne i en source-kö
        if index == 0:
            self._source_queue_busy = True

        if index >= len(paths):
            # Klar med hela kön
            self._source_queue_busy = False
            self._source_queue_current = None
            return

        path = paths[index]
        self._source_queue_current = path

        # Starta detta script
        self._run_bat(path)

        # Nyckeln i self._bat_procs är den resolvade sökvägen (str)
        key = str(Path(path).resolve())

        def check_next():
            proc = self._bat_procs.get(key)

            # Om processen är borta eller klar → dags för nästa
            if proc is None or proc.poll() is not None:
                # Starta nästa i listan
                self._run_bats_sequentially(paths, index + 1)
            else:
                # Inte klar ännu → kolla igen om en liten stund
                QTimer.singleShot(200, check_next)

        # Starta första checken efter en liten stund
        QTimer.singleShot(200, check_next)

    # 0.9-039
    def _show_source_busy_hint(self):
        """
        Visar en snäll notis när användaren trycker Run pending
        medan en source-kö redan kör.
        """
        msg = "Ignoring Run pending: source-kö pågår redan"
        if self._source_queue_current:
            from pathlib import Path           
            name = Path(self._source_queue_current).name
            msg = f"Ignorerar Run pending – jobbar redan med {name}"

        print("[RUN] " + msg)

        # Om du har en info_label: visa kort där också
        if hasattr(self, "info_label") and self.info_label is not None:
            try:
                self.info_label.setText(msg)
                # Vill du vara extra fancy kan du återställa texten efter några sekunder:
                # QTimer.singleShot(3000, lambda: self.info_label.setText(""))
            except Exception:
                pass

    #0.9-037
    def _run_bats_with_delay(self, paths, delay_ms=250, index=0):
        """
        Startar bat-filerna i 'paths' en efter en, med delay_ms mellan varje start.
        Viktigt: den bryr sig INTE om när de blir klara (ingen sekventiell väntan),
        så de kör parallellt efterhand.
        """
        if index >= len(paths):
            return

        path = paths[index]
        self._run_bat(path)

        if index + 1 < len(paths):
            QTimer.singleShot(
                delay_ms,
                lambda: self._run_bats_with_delay(paths, delay_ms, index + 1)
            )

    # 0.9-055 3.4
    def closeEvent(self, event):
        """Spara Tail-bredd och övriga settings när appen stängs."""
        try:
            if hasattr(self, "bat_dock") and self.bat_dock is not None:
                w = int(self.bat_dock.width())
                if w > 0:
                    self._settings_dock_width = w
            self._save_settings()
        except Exception as e:
            print(f"[SETTINGS] closeEvent save failed: {e}")
        super().closeEvent(event)

    #0.9-059
    def _show_selected_bat_text(self):
        """
        Show the currently selected BAT script in a simple read-only text window.
        Triggered by the 'G' shortcut (Gary/Arne special).
        """
        # Hitta vald rad i listan
        it = self.bat_list.currentItem()
        if it is None:
            items = self.bat_list.selectedItems()
            if not items:
                return
            it = items[0]

        path_str = it.data(Qt.UserRole)
        if not path_str:
            return

        p = Path(path_str)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            try:
                QMessageBox.warning(
                    self,
                    "Cannot open script",
                    f"Could not read:\n{p}\n\n{e}"
                )
            except Exception:
                pass
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(p.name)

        layout = QVBoxLayout(dlg)

        editor = QPlainTextEdit()
        editor.setPlainText(text)
        editor.setReadOnly(True)
        # 0.9-085: låt långa ffmpeg-rader brytas automatiskt istället för horisontell scroll
        editor.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        editor.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)


        # Monospace font så ffmpeg-raderna blir läsbara
        font = QFont("Courier New")
        font.setStyleHint(QFont.Monospace)
        editor.setFont(font)

        layout.addWidget(editor)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        dlg.resize(900, 500)
        dlg.exec()


# --- Sanity check for class methods ---
def _debug_check_class_integrity():
    print("\n[DEBUG] Checking VideoApp methods...")
    import inspect
    methods = [name for name, _ in inspect.getmembers(VideoApp, predicate=inspect.isfunction)]
    print("Found methods:", methods)
    if "export_selection" not in methods:
        print("⚠ export_selection() NOT FOUND in VideoApp class — indentation or syntax issue likely.")
    else:
        print("✅ export_selection() present inside VideoApp.")
    print("[DEBUG] End of check\n")

_debug_check_class_integrity()

# 0.89-008 step 2
if __name__ == "__main__":

    os.chdir(RUNTIME_DIR)   # 0.9-089-2 only py mode, if frozen for pyinstaller mode removed

    app = QApplication(sys.argv)

    # Gör texten lite större i Windows så den liknar Linux/Mint-känslan  0.9-015
    # 0.9-066
    try:
        f = app.font()
        size = f.pointSize()

        if IS_WIN:
            # Riktigt Windows: lite större än default
            factor = 1.2
        else:
            # Native Linux/Mac: vi låter Qt:s default vara, du gillar den
            factor = 1.0

        if size > 0:
            f.setPointSize(int(size * factor))
        elif factor != 1.0:
            # fallback om Qt inte ger en rimlig storlek
            base = 10 if not IS_WIN else 11
            f.setPointSize(int(base * factor))

        app.setFont(f)

    except Exception:
        pass

    video_arg = sys.argv[1] if len(sys.argv) > 1 else None
    window = VideoApp(video_arg)
    window.show()

    # If no arg provided, prompt immediately (same UX as Ctrl+O)
    if video_arg is None:
        QTimer.singleShot(0, window.open_video_dialog)

    sys.exit(app.exec())
