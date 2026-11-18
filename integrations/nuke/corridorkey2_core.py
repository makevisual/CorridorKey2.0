"""
corridorkey2_core.py - Nuke-independent logic for the CorridorKey 2.0 node.

Nothing in this file imports nuke. Everything here can be unit tested with a
plain Python interpreter, and none of it changes when Nuke versions change.

Cache layout for one node (one keyed shot):

    <cache_root>/<node_name>/
        v001/
            plate/plate.######.exr          rendered from the node's input
            hint/hint.######.exr            optional hint matte frames
            out/plate_fg_exr/fg_######.exr          (written by the engine)
            out/plate_cutout_exr/cutout_######.exr  (written by the engine)
            engine.log                      full engine output
            manifest.json                   settings, command, status, range
        v002/
            ...

Every run writes a NEW version folder. The node only switches to it after the
engine finishes successfully, so a failed or cancelled run never replaces a
good result, and older versions stay available for rollback.
"""

import json
import os
import re
import sys
import time

ENGINE_ROOT_ENV = "MAKE_CK2_ROOT"      # folder containing infer.py
ENGINE_PYTHON_ENV = "MAKE_CK2_PYTHON"  # optional override for the venv python

PLATE_DIR = "plate"
HINT_DIR = "hint"
OUT_DIR = "out"
MANIFEST = "manifest.json"
ENGINE_LOG = "engine.log"

# The engine names its output folder from the input folder name ("plate"),
# and each frame as <suffix>_<index>.exr with a 0-based, 6-digit index.
ENGINE_OUTPUTS = {
    "fg": ("plate_fg_exr", "fg_%06d.exr"),
    "cutout": ("plate_cutout_exr", "cutout_%06d.exr"),
}

VERSION_RE = re.compile(r"^v(\d{3,})$")
PERCENT_RE = re.compile(r"(\d{1,3})%\|")   # tqdm style "  42%|####"

# Variables Nuke sets for its own embedded Python. If they leak into the
# engine's process, the venv python tries to load Nuke's standard library
# and fails in confusing ways, so they are removed for the child process.
STRIP_ENV = ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONEXECUTABLE")


# ---------------------------------------------------------------------------
# Engine discovery
# ---------------------------------------------------------------------------

def find_engine(environ=None):
    """
    Locate the engine. Returns (engine_root, python_exe, error_or_None).
    """
    env = os.environ if environ is None else environ
    root = env.get(ENGINE_ROOT_ENV, "").strip()
    if not root:
        return None, None, "%s is not set on this machine." % ENGINE_ROOT_ENV
    if not os.path.isfile(os.path.join(root, "infer.py")):
        return None, None, "infer.py not found under %s=%s" % (ENGINE_ROOT_ENV, root)

    python = env.get(ENGINE_PYTHON_ENV, "").strip()
    if not python:
        for rel in (("venv", "Scripts", "python.exe"), ("venv", "bin", "python")):
            cand = os.path.join(root, *rel)
            if os.path.isfile(cand):
                python = cand
                break
    if not python or not os.path.isfile(python):
        return None, None, "No engine python found (expected %s\\venv)." % root
    return root, python, None


def read_engine_version(engine_root):
    """VERSION.json is written by the staging script. Optional."""
    path = os.path.join(engine_root, "VERSION.json")
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def child_env(environ=None):
    """Environment for the engine process, with Nuke's Python vars removed."""
    env = dict(os.environ if environ is None else environ)
    for key in STRIP_ENV:
        env.pop(key, None)
    env["PYTHONUNBUFFERED"] = "1"
    return env


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def build_infer_command(python, engine_root, run_dir, opts):
    """
    Build the infer.py argument list for one run.

    opts keys: fps (float), hint_mode ("none" | "first" | "full"),
               hint_quality (float), despill (float), low_vram (bool),
               exr_dtype ("half" | "float"), temp_dir (str, may be empty)
    """
    plate = os.path.join(run_dir, PLATE_DIR)
    out = os.path.join(run_dir, OUT_DIR)
    cmd = [
        python, "-u", os.path.join(engine_root, "infer.py"),
        "--input", plate,
        "--output_dir", out,
        # Nuke renders the plate in its scene-linear working space, so tell
        # the engine the EXRs are linear rather than display-coded.
        "--frame_dir_linear",
        "--frame_dir_fps", "%g" % float(opts["fps"]),
        "--output_format", "exr",
        "--exr_dtype", opts["exr_dtype"],
        "--outputs", "fg", "cutout",
        "--cutout_linear",
        "--despill_strength", "%g" % float(opts["despill"]),
    ]

    hint_mode = opts.get("hint_mode", "none")
    if hint_mode in ("first", "full"):
        hint = os.path.join(run_dir, HINT_DIR)
        flag = "--hint_first_frame" if hint_mode == "first" else "--hint_video"
        cmd += [flag, hint, "--hint_quality", "%g" % float(opts["hint_quality"])]

    if opts.get("low_vram"):
        cmd.append("--low_vram")
        if opts.get("temp_dir"):
            cmd += ["--temp_dir", opts["temp_dir"]]
    return cmd


# ---------------------------------------------------------------------------
# Version folders
# ---------------------------------------------------------------------------

def list_versions(node_cache_dir):
    """Sorted version labels ('v001', 'v002', ...) that exist on disk."""
    if not os.path.isdir(node_cache_dir):
        return []
    found = []
    for name in os.listdir(node_cache_dir):
        m = VERSION_RE.match(name)
        if m and os.path.isdir(os.path.join(node_cache_dir, name)):
            found.append((int(m.group(1)), name))
    return [name for _, name in sorted(found)]


def next_version(node_cache_dir):
    """Label for the next version folder. Does not create it."""
    existing = list_versions(node_cache_dir)
    n = int(VERSION_RE.match(existing[-1]).group(1)) + 1 if existing else 1
    return "v%03d" % n


def good_versions(node_cache_dir):
    """Versions whose manifest says the engine finished successfully."""
    return [v for v in list_versions(node_cache_dir)
            if read_manifest(os.path.join(node_cache_dir, v)).get("status") == "ok"]


def make_run_dirs(run_dir, with_hint):
    for sub in [PLATE_DIR, OUT_DIR] + ([HINT_DIR] if with_hint else []):
        os.makedirs(os.path.join(run_dir, sub), exist_ok=True)


def plate_pattern(run_dir):
    """Write path for the plate, Nuke style (forward slashes, ###### frames)."""
    return to_nuke_path(os.path.join(run_dir, PLATE_DIR, "plate.######.exr"))


def hint_pattern(run_dir):
    return to_nuke_path(os.path.join(run_dir, HINT_DIR, "hint.######.exr"))


def output_pattern(run_dir, name):
    """Read path for one engine output, printf style (%06d)."""
    folder, frame = ENGINE_OUTPUTS[name]
    return to_nuke_path(os.path.join(run_dir, OUT_DIR, folder, frame))


def count_output_frames(run_dir, name):
    folder, _ = ENGINE_OUTPUTS[name]
    path = os.path.join(run_dir, OUT_DIR, folder)
    if not os.path.isdir(path):
        return 0
    return len([f for f in os.listdir(path) if f.lower().endswith(".exr")])


def to_nuke_path(path):
    return path.replace("\\", "/")


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def read_manifest(run_dir):
    try:
        with open(os.path.join(run_dir, MANIFEST), "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_manifest(run_dir, data):
    """Write via a temp file and rename, so a crash never leaves half a file."""
    path = os.path.join(run_dir, MANIFEST)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def new_manifest(version, first, last, opts, cmd, engine_root):
    return {
        "version": version,
        "status": "running",
        "first_frame": int(first),
        "last_frame": int(last),
        "options": dict(opts),
        "command": list(cmd),
        "engine_root": engine_root,
        "engine_version": read_engine_version(engine_root),
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "user": os.environ.get("USERNAME") or os.environ.get("USER", ""),
        "host": os.environ.get("COMPUTERNAME", ""),
    }


# ---------------------------------------------------------------------------
# Progress parsing
# ---------------------------------------------------------------------------

def parse_progress(text):
    """Last tqdm percentage in a chunk of output, or None."""
    hits = PERCENT_RE.findall(text)
    if not hits:
        return None
    return max(0, min(100, int(hits[-1])))


def tail(text, lines=15):
    return "\n".join(text.strip().splitlines()[-lines:])


# ---------------------------------------------------------------------------
# Process helper
# ---------------------------------------------------------------------------

def popen_kwargs():
    """Hide the console window the engine would otherwise open on Windows."""
    if sys.platform == "win32":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}
