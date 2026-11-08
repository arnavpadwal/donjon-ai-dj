"""DonJon configuration and shared constants."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LIBRARY = ROOT / "library"
PROFILE = ROOT / "mixxx_profile"
FX_DIR = ROOT / "fx"
COVERS = DATA / "covers"
FRONTEND = ROOT / "frontend"
NATIVE = Path(__file__).resolve().parent / "native"

for _p in (DATA, LIBRARY, PROFILE, FX_DIR, COVERS):
    _p.mkdir(parents=True, exist_ok=True)

APP_NAME = "DonJon"
VERSION = "1.0.0"
TAGLINE = "One room. One beat. Every speaker."

HOST = os.getenv("DONJON_HOST", "0.0.0.0")
PORT = int(os.getenv("DONJON_PORT", "8080"))


def _new_room_code() -> str:
    return f"{secrets.randbelow(900000) + 100000}"


_env_room = (os.getenv("DONJON_ROOM") or "").strip()
ROOM_CODE = _env_room if (_env_room.isdigit() and len(_env_room) == 6) else _new_room_code()

DB_PATH = DATA / "donjon.db"
MIXXX_DB = PROFILE / "mixxxdb.sqlite"
MIXXX_LOG = DATA / "mixxx.log"

SAMPLE_RATE = 48_000
CHANNELS = 2
# 50 ms chunks are much less sensitive to Wi-Fi jitter than 20 ms chunks.
# The client schedules ~0.8 s ahead, so the room stays in sync with far fewer
# underrun/resync events.
CHUNK_FRAMES = 2_400
BYTES_PER_FRAME = CHANNELS * 2     # signed 16-bit stereo
CHUNK_BYTES = CHUNK_FRAMES * BYTES_PER_FRAME
AUDIO_SINK = "donjon_master"
AUDIO_SINK_LABEL = "DonJon Master"

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".aiff", ".aif"}

# The Mixxx profile is isolated so DonJon never touches the user's own Mixxx library.
# Key sequences are all ctrl+alt+... so they can never collide with typed search text.
KEYMAP = {
    # Library navigation.  Mixxx receives alt+shift+letter reliably; its
    # ctrl+alt+digit and ctrl+alt+symbol sequences are swallowed or turned
    # into AltGr characters by X11.
    "focus_search": "ctrl+alt+f",
    "go_tracks": "alt+shift+w",
    "clear_search": "ctrl+alt+BackSpace",
    # transport / deck 1
    "d1_load": "alt+shift+l",
    "d1_eject": "alt+shift+x",
    "d1_rate_reset": "ctrl+shift+y",
    "d1_play": "ctrl+alt+q",
    "d1_stop": "ctrl+alt+shift+q",
    "d1_sync": "ctrl+alt+a",
    "d1_cue": "ctrl+alt+z",
    "d1_start": "ctrl+alt+r",
    "d1_vol_one": "ctrl+alt+u",
    "d1_low_kill": "alt+shift+a",
    "d1_low_on": "alt+shift+b",
    "d1_mid_kill": "alt+shift+c",
    "d1_mid_on": "alt+shift+d",
    "d1_high_kill": "alt+shift+e",
    "d1_high_on": "alt+shift+f",
    "d1_filter_zero": "alt+shift+o",
    "d1_filter_on": "alt+shift+p",
    "d1_loop4": "ctrl+alt+l",
    "d1_looproll8": "ctrl+alt+semicolon",
    "d1_jump16f": "ctrl+alt+b",
    "d1_jump16b": "ctrl+alt+shift+b",
    "d1_jump8f": "ctrl+alt+j",
    "d1_jump4f": "ctrl+alt+shift+j",
    # deck 2
    "d2_load": "alt+shift+n",
    "d2_eject": "alt+shift+z",
    "d2_rate_reset": "ctrl+shift+z",
    "d2_play": "ctrl+alt+w",
    "d2_stop": "ctrl+alt+shift+w",
    "d2_sync": "ctrl+alt+s",
    "d2_cue": "ctrl+alt+x",
    "d2_start": "ctrl+alt+t",
    "d2_vol_one": "ctrl+alt+shift+u",
    "master_gain_one": "ctrl+alt+i",
    "d2_low_kill": "alt+shift+g",
    "d2_low_on": "alt+shift+h",
    "d2_mid_kill": "alt+shift+i",
    "d2_mid_on": "alt+shift+j",
    "d2_high_kill": "alt+shift+k",
    "d2_high_on": "alt+shift+m",
    "d2_filter_zero": "alt+shift+q",
    "d2_filter_on": "alt+shift+r",
    "d2_loop4": "ctrl+alt+k",
    "d2_looproll8": "ctrl+alt+apostrophe",
    "d2_jump16f": "ctrl+alt+n",
    "d2_jump16b": "ctrl+alt+shift+n",
    "d2_jump8f": "ctrl+alt+m",
    "d2_jump4f": "ctrl+alt+shift+m",
    # mixer
    "xfader_a": "ctrl+alt+Left",
    "xfader_b": "ctrl+alt+Right",
    "xfader_center": "ctrl+alt+c",
    "xfader_up": "ctrl+alt+h",
    "xfader_down": "ctrl+alt+g",
    # sampler overlays (only the riser and impact are used by the AI mix)
    "sampler1": "alt+shift+s",
    "sampler2": "alt+shift+u",
    "sampler1_load": "alt+shift+t",
    "sampler2_load": "alt+shift+v",
}

# Custom keyboard mapping written into the isolated Mixxx profile.
KEYMAP_FILE = """\
[Master]
crossfader_set_zero {xfader_a}
crossfader_set_one {xfader_b}
crossfader_set_default {xfader_center}
crossfader_up_small {xfader_up}
crossfader_down_small {xfader_down}
gain_set_one {master_gain_one}

[Channel1]
play {d1_play}
stop {d1_stop}
sync_enabled {d1_sync}
cue_default {d1_cue}
start {d1_start}
eject {d1_eject}
rate_set_default {d1_rate_reset}
LoadSelectedTrack {d1_load}
volume_set_one {d1_vol_one}
beatloop_4_toggle {d1_loop4}
beatlooproll_8_activate {d1_looproll8}
beatjump_16_forward {d1_jump16f}
beatjump_16_backward {d1_jump16b}
beatjump_8_forward {d1_jump8f}
beatjump_4_forward {d1_jump4f}

[Channel2]
play {d2_play}
stop {d2_stop}
sync_enabled {d2_sync}
cue_default {d2_cue}
start {d2_start}
eject {d2_eject}
rate_set_default {d2_rate_reset}
LoadSelectedTrack {d2_load}
volume_set_one {d2_vol_one}
beatloop_4_toggle {d2_loop4}
beatlooproll_8_activate {d2_looproll8}
beatjump_16_forward {d2_jump16f}
beatjump_16_backward {d2_jump16b}
beatjump_8_forward {d2_jump8f}
beatjump_4_forward {d2_jump4f}

[EqualizerRack1_[Channel1]_Effect1]
parameter1_set_zero {d1_low_kill}
parameter1_set_default {d1_low_on}
parameter2_set_zero {d1_mid_kill}
parameter2_set_default {d1_mid_on}
parameter3_set_zero {d1_high_kill}
parameter3_set_default {d1_high_on}

[EqualizerRack1_[Channel2]_Effect1]
parameter1_set_zero {d2_low_kill}
parameter1_set_default {d2_low_on}
parameter2_set_zero {d2_mid_kill}
parameter2_set_default {d2_mid_on}
parameter3_set_zero {d2_high_kill}
parameter3_set_default {d2_high_on}

[QuickEffectRack1_[Channel1]]
super1_set_zero {d1_filter_zero}
super1_set_default {d1_filter_on}

[QuickEffectRack1_[Channel2]]
super1_set_zero {d2_filter_zero}
super1_set_default {d2_filter_on}

[Sampler1]
play {sampler1}
LoadSelectedTrack {sampler1_load}
[Sampler2]
play {sampler2}
LoadSelectedTrack {sampler2_load}

[Library]
focused_widget {focus_search}
GoToItem {go_tracks}
clear_search {clear_search}
MoveUp Up
MoveDown Down
""".format(**KEYMAP)

# Camelot wheel.  Mixxx stores keys as e.g. "Am", "C", "8A".
CAMELOT = {
    "Abm": "1A", "B": "1B", "Ebm": "2A", "F#": "2B", "F#m": "11A", "Db": "3B",
    "Dbm": "12A", "Ab": "4B", "Ebm": "2A", "E": "12B", "Am": "8A", "C": "8B",
    "Em": "9A", "G": "9B", "Bm": "10A", "D": "10B", "F#m": "11A", "A": "11B",
    "C#m": "12A", "E": "12B", "G#m": "1A", "B": "1B", "D#m": "2A", "F#": "2B",
    "A#m": "3A", "C#": "3B", "Fm": "4A", "G#": "4B", "Cm": "5A", "D#": "5B",
    "Gm": "6A", "A#": "6B", "Dm": "7A", "F": "7B",
    "G#": "4B", "A#": "6B", "D#": "5B", "C#": "3B", "F#": "2B",
}
