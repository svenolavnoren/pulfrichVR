#!/usr/bin/env python3
import sys
import re
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout,
    QListWidget, QListWidgetItem,
    QPushButton, QDoubleSpinBox, QLabel, QFileDialog, QMessageBox
)
from PySide6.QtCore import Qt

# --- Konstanter ---
TARGET_RES = 5760
FPS = 24
DEFAULT_TRIM_SAFETY = 2.0  # sekunder att kapa från insv-slut om vi hittar duration

@dataclass
class Scene:
    meta_line: str
    video_path: str          # t.ex. VID_20260215_112243_00_206.mov
    insv_path: Optional[str] # t.ex. VID_20260215_112243_00_206.insv (eller None)
    duration: float          # sekunder
    output_name: str         # t.ex. 260215_206__00011_00014__360TB.mp4
    raw_ffmpeg_line: str     # original jpg-ffmpeg-rad från VR...bat
    source_bat: Optional[str] = None  # var scenen kom ifrån (VR_*.bat)

    def label(self) -> str:
        """Text som visas i listan."""
        return f"{self.output_name}  |  {self.duration:.1f} s  |  {self.video_path}"


# --- Hjälpfunktioner för ffprobe / parsing ---

def probe_insv_duration(insv_path: Path) -> Optional[float]:
    """Returnera duration (sekunder) för insv, eller None om det inte funkar."""
    if not insv_path.exists():
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json", str(insv_path)
            ],
            capture_output=True,
            text=True,
            check=True
        )
        data = json.loads(result.stdout)
        dur_str = data["format"]["duration"]
        dur = float(dur_str)
        return dur
    except Exception as e:
        print(f"Varning: kunde inte läsa duration från {insv_path}: {e}")
        return None


META_RE = re.compile(
    r'#\s*VR_META\s+video="([^"]+)"\s+left=(\d+)\s+right=(\d+)\s+dir_px=(\d+)\s+res_px=(\d+)'
)

def parse_meta_line(line: str):
    """Returnera dict med fält från VR_META-rad."""
    m = META_RE.match(line.strip())
    if not m:
        raise ValueError(f"Kunde inte tolka VR_META-rad: {line!r}")
    return {
        "video": m.group(1),
        "left": int(m.group(2)),
        "right": int(m.group(3)),
        "dir_px": int(m.group(4)),
        "res_px": int(m.group(5)),
    }


def extract_output_basename_from_ffmpeg(line: str) -> str:
    """
    Hitta namnet på utdatafilen i ett jpg-skript, utan ändelse.
    Antag struktur: ... "NAMN__360TB.jpg"
    """
    m = re.search(r'"([^"]+)\.jpg"', line)
    if not m:
        raise ValueError(f"Hittar ingen .jpg-utfil i ffmpeg-raden: {line}")
    return m.group(1)


def transform_ffmpeg_line_to_mp4(scene: Scene) -> str:
    """
    Ta original jpg-ffmpeg-kommandot och omvandla det till:
    - 2 eller 3 inputs (-i mov -i mov [-i insv])
    - [left_rot][right_rot]hstack -> hstack + scale=5760:5760 + format=yuv420p + tpad
    - global -t <duration> så både video och ljud klipps till samma längd
    - mapar video + ev. ljud
    - output mp4 med libsvtav1 + aac
    - -hide_banner -n
    """
    line = scene.raw_ffmpeg_line.strip()
    duration = scene.duration

    # 1) Byt till -n (ingen overwrite)
    line = line.replace("-hide_banner -y", "-hide_banner -n")
    line = line.replace("-y ", "-n ")

    # 2) Lägg till insv-input och -t <duration>
    if scene.insv_path:
        # från: ... -i MOV -i MOV -filter_complex ...
        # till: ... -i MOV -i MOV -i INSV -t D -filter_complex ...
        line = line.replace(
            " -filter_complex",
            f' -i "{scene.insv_path}" -t {duration} -filter_complex'
        )
    else:
        # Ingen insv: lägg bara -t D innan filter_complex
        line = line.replace(
            " -filter_complex",
            f' -t {duration} -filter_complex'
        )

    # 3) Modifiera filter-svansen: lägg in scale + yuv420p + tpad
    repl = (
        f'[left_rot][right_rot]hstack,'
        f'scale={TARGET_RES}:{TARGET_RES},'
        f'format=yuv420p,'
        f'tpad=stop_mode=clone:stop_duration={duration}[v3d]"'
    )
    line = line.replace('[left_rot][right_rot]hstack[v3d]"', repl)

    # 4) Lägg till audio-map om vi har insv
    if scene.insv_path:
        # video är [v3d], ljud från tredje input (index 2) => "2:a"
        # egen ändring 2:a -> 1:a enl pulfrichVR_0.9-087
        line = line.replace('-map "[v3d]"', '-map "[v3d]" -map "1:a"')
    
    # annars låter vi befintlig map vara som den är

    # 5) Ersätt stillbilds-utgången med mp4-utgång + fps + codecs
    #   Original: -frames:v 1 -update 1 -q:v 1 "NAMN.jpg"
    #   Nytt:     -r FPS -c:v libsvtav1 [-c:a aac ...] "NAMN.mp4"
    def repl_out(m):
        base = m.group(1)  # NAMN utan .jpg
        if scene.insv_path:
            audio_part = '-c:a aac -b:a 128k '
        else:
            audio_part = ''
        return f'-r {FPS} -c:v libsvtav1 {audio_part}"{base}.mp4"'

    line = re.sub(
        r'-frames:v\s+1\s+-update\s+1\s+-q:v\s+1\s+"([^"]+)\.jpg"',
        repl_out,
        line
    )

    return line


def create_scene_from_vr_bat(bat_path: Path) -> Scene:
    """
    Läs första VR_META-rad + första ffmpeg-rad i en VR...bat och bygg en Scene.
    """
    text = bat_path.read_text(encoding="utf-8", errors="ignore").splitlines()

    meta_line = None
    ff_line = None

    for i, line in enumerate(text):
        if line.strip().startswith("# VR_META"):
            meta_line = line.rstrip("\n")
            # leta efter första ffmpeg-rad efteråt
            for j in range(i + 1, len(text)):
                l2 = text[j].strip()
                if l2.startswith("ffmpeg "):
                    ff_line = text[j].rstrip("\n")
                    break
            break

    if not meta_line or not ff_line:
        raise ValueError(f"Hittade inte VR_META + ffmpeg i {bat_path}")

    meta = parse_meta_line(meta_line)
    video = meta["video"]

    # output-basnamn från jpg-utdata i ffmpeg-raden
    base = extract_output_basename_from_ffmpeg(ff_line)
    output_name = base + ".mp4"

    # insv-fil bredvid .mov
    mov_path = Path(video)
    insv_path = mov_path.with_suffix(".insv")
    if not insv_path.exists():
        # prova utan katalog, ifall meta bara hade filnamn
        insv_path = bat_path.parent / insv_path.name

    if insv_path.exists():
        insv_dur = probe_insv_duration(insv_path)
    else:
        insv_dur = None

    if insv_dur and insv_dur > DEFAULT_TRIM_SAFETY + 1:
        duration = insv_dur - DEFAULT_TRIM_SAFETY
    else:
        duration = 8.0  # fallback

    return Scene(
        meta_line=meta_line,
        video_path=video,
        insv_path=str(insv_path) if insv_path.exists() else None,
        duration=duration,
        output_name=output_name,
        raw_ffmpeg_line=ff_line,
        source_bat=str(bat_path),
    )


# --- GUI-klient ---

class VRStoryWindow(QMainWindow):
    def __init__(self, start_dir: Path):
        super().__init__()
        self.setWindowTitle("VRstory003 – bildmanus-editor")
        self.start_dir = start_dir
        self.scenes: List[Scene] = []

        self._init_ui()

        # Första start: försök hitta VR*.bat och ladda in dem
        self.auto_load_vr_bats()

    # ----- dynamiska filnamn baserade på VR_yyMMdd_nnn__*.bat -----

    def compute_story_base_name(self) -> str:
        """
        Bygg ett basnamn typ '260223_236-240' baserat på sceners source_bat:
        - datum = högsta (senaste) YYMMDD
        - indexintervall = min_nnn - max_nnn
        Om vi inte hittar något mönster, fall tillbaka till 'VRstory'.
        """
        dates = []
        idxs = []

        for sc in self.scenes:
            if not sc.source_bat:
                continue
            stem = Path(sc.source_bat).stem  # t.ex. VR_260223_236__00013_00017
            m = re.match(r"VR_(\d{6})_(\d{3})__", stem)
            if not m:
                continue
            dates.append(m.group(1))           # '260223'
            idxs.append(int(m.group(2)))       # 236

        if dates and idxs:
            max_date = max(dates)
            min_idx = min(idxs)
            max_idx = max(idxs)
            # utan nollpadding på intervallet, som i ditt exempel 236-240
            return f"{max_date}_{min_idx}-{max_idx}"
        else:
            return "VRstory"

    def get_story_filenames(self):
        """
        Returnera dict med 'base', 'playlist', 'script', 'output'
        baserat på nuvarande scener.
        """
        base = self.compute_story_base_name()
        return {
            "base": base,
            "playlist": f"{base}_playlist.txt",
            "script": f"{base}_bildmanus.sh",
            "output": f"{base}.VRstory.mp4",
        }

    # ----- UI-setup -----

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self.scene_list = QListWidget()
        layout.addWidget(self.scene_list)

        # Duration-redigering
        dur_layout = QHBoxLayout()
        dur_layout.addWidget(QLabel("Duration (s):"))
        self.dur_spin = QDoubleSpinBox()
        self.dur_spin.setDecimals(1)
        self.dur_spin.setRange(1.0, 600.0)
        self.dur_spin.setSingleStep(0.5)
        dur_layout.addWidget(self.dur_spin)
        self.apply_dur_btn = QPushButton("Sätt duration")
        dur_layout.addWidget(self.apply_dur_btn)
        layout.addLayout(dur_layout)

        # Flytta / ta bort
        btn_layout = QHBoxLayout()
        self.up_btn = QPushButton("Flytta upp")
        self.down_btn = QPushButton("Flytta ned")
        self.del_btn = QPushButton("Ta bort")
        btn_layout.addWidget(self.up_btn)
        btn_layout.addWidget(self.down_btn)
        btn_layout.addWidget(self.del_btn)
        layout.addLayout(btn_layout)

        # Fil / Render
        file_layout = QHBoxLayout()
        self.add_bat_btn = QPushButton("Lägg till från VR…-bat")
        self.save_script_btn = QPushButton("Spara bildmanus.sh")
        self.render_btn = QPushButton("Rendera VRstory.mp4")
        file_layout.addWidget(self.add_bat_btn)
        file_layout.addWidget(self.save_script_btn)
        file_layout.addWidget(self.render_btn)
        layout.addLayout(file_layout)

        # Kopplingar
        self.scene_list.currentRowChanged.connect(self.on_scene_selected)
        self.apply_dur_btn.clicked.connect(self.on_apply_duration)
        self.up_btn.clicked.connect(self.on_move_up)
        self.down_btn.clicked.connect(self.on_move_down)
        self.del_btn.clicked.connect(self.on_delete_scene)
        self.add_bat_btn.clicked.connect(self.on_add_from_bat)
        self.save_script_btn.clicked.connect(self.on_save_script)
        self.render_btn.clicked.connect(self.on_render_clicked)

    # --- Laddning / uppdatering av lista ---

    def auto_load_vr_bats(self):
        bats = sorted(self.start_dir.glob("VR*.bat"))
        for bat in bats:
            try:
                scene = create_scene_from_vr_bat(bat)
                self.scenes.append(scene)
            except Exception as e:
                print(f"Hoppar över {bat}: {e}")

        self.refresh_scene_list()

    def refresh_scene_list(self):
        self.scene_list.clear()
        for sc in self.scenes:
            item = QListWidgetItem(sc.label())
            self.scene_list.addItem(item)

    def on_scene_selected(self, row: int):
        if 0 <= row < len(self.scenes):
            sc = self.scenes[row]
            self.dur_spin.setValue(sc.duration)

    def on_apply_duration(self):
        row = self.scene_list.currentRow()
        if 0 <= row < len(self.scenes):
            self.scenes[row].duration = float(self.dur_spin.value())
            self.refresh_scene_list()
            self.scene_list.setCurrentRow(row)

    def on_move_up(self):
        row = self.scene_list.currentRow()
        if row > 0:
            self.scenes[row - 1], self.scenes[row] = self.scenes[row], self.scenes[row - 1]
            self.refresh_scene_list()
            self.scene_list.setCurrentRow(row - 1)

    def on_move_down(self):
        row = self.scene_list.currentRow()
        if 0 <= row < len(self.scenes) - 1:
            self.scenes[row + 1], self.scenes[row] = self.scenes[row], self.scenes[row + 1]
            self.refresh_scene_list()
            self.scene_list.setCurrentRow(row + 1)

    def on_delete_scene(self):
        row = self.scene_list.currentRow()
        if 0 <= row < len(self.scenes):
            del self.scenes[row]
            self.refresh_scene_list()
            if self.scenes:
                self.scene_list.setCurrentRow(min(row, len(self.scenes) - 1))

    def on_add_from_bat(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Välj VR…-bat-filer", str(self.start_dir), "Batch-filer (*.bat);;Alla filer (*)"
        )
        for p in paths:
            try:
                scene = create_scene_from_vr_bat(Path(p))
                self.scenes.append(scene)
            except Exception as e:
                QMessageBox.warning(self, "Fel vid tolkning", f"Fil: {p}\n\n{e}")
        self.refresh_scene_list()

    # --- Bygga manus + playlist ---

    def build_bildmanus_text(self) -> str:
        names = self.get_story_filenames()
        playlist_name = names["playlist"]
        output_name = names["output"]

        lines = []
        lines.append("#!/usr/bin/env bash")
        lines.append("")
        # medvetet inte set -e här; -n i ffmpeg får gärna “misslyckas” när filer finns
        lines.append('SCRIPT="${BASH_SOURCE[0]:-$0}"')
        lines.append(f'PLAYLIST="{playlist_name}"')
        lines.append(f'OUTPUT="{output_name}"')
        lines.append("")
        lines.append("########################################")
        lines.append("# SCENER (autogenererat av VRstory003)")
        lines.append("########################################")
        lines.append("")

        for sc in self.scenes:
            # VR_META-rad
            lines.append(sc.meta_line)
            # SCENE-rad (för playlist-byggaren)
            lines.append(f"# SCENE: {sc.output_name}")
            # ffmpeg-rad
            ff_line = transform_ffmpeg_line_to_mp4(sc)
            lines.append(ff_line)
            lines.append("")

        # Autodel: bygg playlist + concat
        lines.append("########################################")
        lines.append("# AUTO: bygg playlist + concat")
        lines.append("########################################")
        lines.append("")
        lines.append('grep \'^# SCENE:\' "$SCRIPT" \\')
        lines.append('  | sed -E "s/^# SCENE:[[:space:]]*/file \'/" \\')
        lines.append('  | sed -E "s/$/\'/" \\')
        lines.append('  > "$PLAYLIST"')
        lines.append("")
        lines.append('echo "Skapade $PLAYLIST:"')
        lines.append('cat "$PLAYLIST"')
        lines.append("")
        lines.append("set -e")
        lines.append('ffmpeg -hide_banner -y \\')
        lines.append('  -f concat -safe 0 -i "$PLAYLIST" \\')
        lines.append('  -c copy \\')
        lines.append('  "$OUTPUT"')
        lines.append("")
        lines.append('echo "Klar: $OUTPUT"')
        lines.append("")

        return "\n".join(lines)

    def write_playlist_file(self):
        names = self.get_story_filenames()
        playlist_name = names["playlist"]
        playlist_lines = [f"file '{sc.output_name}'" for sc in self.scenes]
        Path(self.start_dir / playlist_name).write_text(
            "\n".join(playlist_lines) + "\n", encoding="utf-8"
        )

    # --- Kommandon ---

    def on_save_script(self):
        if not self.scenes:
            QMessageBox.information(self, "Inget att spara", "Det finns inga scener ännu.")
            return
        text = self.build_bildmanus_text()
        names = self.get_story_filenames()
        script_path = self.start_dir / names["script"]
        script_path.write_text(text, encoding="utf-8")
        QMessageBox.information(self, "Sparat", f"Skrev {script_path}")

    def on_render_clicked(self):
        if not self.scenes:
            QMessageBox.information(self, "Inget att rendera", "Det finns inga scener ännu.")
            return

        names = self.get_story_filenames()
        playlist_name = names["playlist"]
        output_name = names["output"]

        # 1) Spara manus (så du alltid har en textkopia av läget)
        self.on_save_script()

        # 2) Skapa playlist.txt
        self.write_playlist_file()

        # 3) Rendera varje scen med ffmpeg -n (bara de som saknas körs)
        for sc in self.scenes:
            out_path = self.start_dir / sc.output_name
            if out_path.exists():
                print(f"Hoppar scen {out_path}, finns redan.")
                continue
            cmd = transform_ffmpeg_line_to_mp4(sc)
            print(f"Kör scen: {cmd}")
            # Kör via bash -lc för att låta ffmpeg-raden vara som den är
            proc = subprocess.run(
                ["bash", "-lc", cmd],
                cwd=self.start_dir
            )
            if proc.returncode != 0:
                QMessageBox.warning(self, "Fel vid ffmpeg",
                                    f"ffmpeg misslyckades för scen {sc.output_name}.\n"
                                    f"Se konsollen för detaljer.")
                return

        # 4) Concat till slutfilm
        concat_cmd = [
            "ffmpeg", "-hide_banner", "-y",
            "-f", "concat", "-safe", "0",
            "-i", playlist_name,
            "-c", "copy",
            output_name,
        ]
        print("Kör concat:", " ".join(concat_cmd))
        proc = subprocess.run(concat_cmd, cwd=self.start_dir)
        if proc.returncode != 0:
            QMessageBox.warning(self, "Fel vid concat",
                                "Concat-ffmpeg misslyckades. Se konsollen.")
            return

        QMessageBox.information(self, "Klart", f"Skapade {output_name}")


def main():
    app = QApplication(sys.argv)
    start_dir = Path.cwd()
    win = VRStoryWindow(start_dir)
    win.resize(900, 600)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
