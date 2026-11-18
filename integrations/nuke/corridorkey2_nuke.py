"""
corridorkey2_nuke.py - CorridorKey 2.0 node for Nuke (Python Group, no compiled code).

How it works
------------
The node is a Group. Pressing "Key Shot":
  1. renders the Source input (and optional Hint input) to EXRs in a new
     version folder (see corridorkey2_core for the layout),
  2. runs the CorridorKey 2.0 engine (infer.py, in its own venv) on those
     frames in a background process, with a progress bar and cancel,
  3. on success, points the Group's internal Read nodes at the result.

The saved Group only contains Reads, a Copy, Switches and a Premult, so a
script renders anywhere (farm included) without the engine installed. Only
pressing "Key Shot" needs a CUDA GPU and the engine.

Why a Python Group and not a compiled plugin: the 2.0 model analyses the
whole clip at once (time awareness), so it cannot run frame-by-frame inside
Nuke's render loop anyway, and plain Python needs no rebuild per Nuke version.
"""

import os
import subprocess
import threading

import nuke

import corridorkey2_core as core

NODE_CLASS_LABEL = "CorridorKey2"
RESULT_CHOICES = ["despilled (engine)", "raw fg + alpha"]
HINT_CHOICES = ["none", "first frame", "full sequence"]
HINT_MODE_MAP = {"none": "none", "first frame": "first", "full sequence": "full"}

# Full node names with a run in progress, so one node cannot run twice.
_RUNNING = set()

# Stored on the Group. Wrapped so a machine without this module (farm,
# freelancer) can still open and render the script with no errors.
_KNOB_CHANGED = (
    "try:\n"
    "    import corridorkey2_nuke\n"
    "    corridorkey2_nuke.knob_changed(nuke.thisNode(), nuke.thisKnob())\n"
    "except ImportError:\n"
    "    pass\n"
)


# ---------------------------------------------------------------------------
# Node creation
# ---------------------------------------------------------------------------

def create():
    """Menu entry point. Builds a new CorridorKey2 Group."""
    grp = nuke.createNode("Group", inpanel=False)
    grp.setName(NODE_CLASS_LABEL, uncollide=True)
    grp["tile_color"].setValue(0x3A9A4AFF)
    grp["label"].setValue("[value version]")
    _add_knobs(grp)
    _build_internals(grp)
    grp["knobChanged"].setValue(_KNOB_CHANGED)
    use_input_range(grp)
    _set_status(grp)
    grp.showControlPanel()
    return grp


def _add_knobs(grp):
    def add(knob, newline=True, tooltip=None):
        if not newline:
            knob.clearFlag(nuke.STARTLINE)
        if tooltip:
            knob.setTooltip(tooltip)
        grp.addKnob(knob)
        return knob

    add(nuke.Tab_Knob("ck2_tab", "CorridorKey2"))
    add(nuke.Text_Knob("status", "Engine", ""))

    add(nuke.Text_Knob("div_run", "Run"))
    add(nuke.File_Knob("cache_root", "cache root"),
        tooltip="Where rendered plates and results are kept. Must be a shared "
                "path if the script will render on the farm.")
    grp["cache_root"].setValue("[file dirname [value root.name]]/CorridorKey2")
    add(nuke.Int_Knob("frame_first", "frame range"))
    add(nuke.Int_Knob("frame_last", ""), newline=False)
    add(nuke.PyScript_Knob("use_range", "use input range",
                           "import corridorkey2_nuke as ck; ck.use_input_range(nuke.thisNode())"),
        newline=False)

    add(nuke.Enumeration_Knob("hint_mode", "hint", HINT_CHOICES),
        tooltip="Uses the Hint input (white = subject). 'first frame' only "
                "guides the start of the clip.")
    add(nuke.Enumeration_Knob("hint_channel", "hint channel", ["alpha", "red"]), newline=False)
    k = add(nuke.Double_Knob("hint_quality", "hint quality"),
            tooltip="How much the model trusts the hint. 0.95 clean matte, "
                    "0.75 rough automated matte, 0.5 or lower loose guide.")
    k.setRange(0.0, 1.0)
    k.setValue(0.95)
    k = add(nuke.Double_Knob("despill", "despill"),
            tooltip="Baked in at run time. Only affects the 'despilled' result.")
    k.setRange(0.0, 1.0)
    k.setValue(0.5)
    add(nuke.Enumeration_Knob("exr_dtype", "exr type", ["half", "float"]))
    add(nuke.Boolean_Knob("low_vram", "low VRAM"), newline=False)
    add(nuke.File_Knob("temp_dir", "low VRAM temp"),
        tooltip="Local SSD folder for low VRAM mode. Empty = system temp.")
    add(nuke.PyScript_Knob("run", "Key Shot",
                           "import corridorkey2_nuke as ck; ck.run(nuke.thisNode())"))

    add(nuke.Text_Knob("div_out", "Output"))
    add(nuke.Enumeration_Knob("version", "version", ["none"]),
        tooltip="Successful runs. Switch to roll back to an earlier key.")
    add(nuke.PyScript_Knob("refresh", "refresh",
                           "import corridorkey2_nuke as ck; ck.refresh_versions(nuke.thisNode())"),
        newline=False)
    add(nuke.PyScript_Knob("open_cache", "open folder",
                           "import corridorkey2_nuke as ck; ck.open_cache_folder(nuke.thisNode())"),
        newline=False)
    add(nuke.Enumeration_Knob("result", "result", RESULT_CHOICES))
    k = add(nuke.Boolean_Knob("premult", "premultiply"), newline=False)
    k.setValue(True)

    # Hidden state: whether a result is loaded. Drives the passthrough switch.
    k = add(nuke.Boolean_Knob("has_result", "has result"))
    k.setVisible(False)


def _build_internals(grp):
    """
    Internal graph:
        Read_fg + Read_cutout -> CopyAlpha (cutout alpha onto raw fg)
        PickResult  = Switch(Read_cutout, CopyAlpha)  by knob 'result'
        Premult     = disabled when knob 'premult' is off
        HasResult   = Switch(Source, Premult)         by hidden 'has_result'
        Output      <- HasResult
    The Hint input is only read while rendering hint frames for a run.
    """
    grp.begin()
    try:
        src = nuke.nodes.Input(name="Source")      # input 0
        nuke.nodes.Input(name="Hint")              # input 1
        read_fg = nuke.nodes.Read(name="Read_fg")
        read_cut = nuke.nodes.Read(name="Read_cutout")
        for r in (read_fg, read_cut):
            # Linear in, linear out: no colorspace conversion on read.
            r["raw"].setValue(True)
            r["on_error"].setValue("black")

        copy = nuke.nodes.Copy(name="CopyAlpha", from0="rgba.alpha", to0="rgba.alpha")
        copy.setInput(0, read_fg)    # B
        copy.setInput(1, read_cut)   # A

        pick = nuke.nodes.Switch(name="PickResult")
        pick.setInput(0, read_cut)
        pick.setInput(1, copy)
        pick["which"].setExpression("parent.result")

        pm = nuke.nodes.Premult(name="Premult")
        pm.setInput(0, pick)
        pm["disable"].setExpression("!parent.premult")

        out_sw = nuke.nodes.Switch(name="HasResult")
        out_sw.setInput(0, src)
        out_sw.setInput(1, pm)
        out_sw["which"].setExpression("parent.has_result")

        out = nuke.nodes.Output()
        out.setInput(0, out_sw)
    finally:
        grp.end()


# ---------------------------------------------------------------------------
# Knob callbacks
# ---------------------------------------------------------------------------

def knob_changed(grp, knob):
    if knob is None:
        return
    if knob.name() == "version":
        apply_version(grp, knob.value())
    elif knob.name() == "showPanel":
        _set_status(grp)


def use_input_range(grp):
    src = grp.input(0)
    if src is not None:
        first, last = src.firstFrame(), src.lastFrame()
    else:
        first, last = int(nuke.root()["first_frame"].value()), int(nuke.root()["last_frame"].value())
    grp["frame_first"].setValue(first)
    grp["frame_last"].setValue(last)


def open_cache_folder(grp):
    path = _node_cache_dir(grp, quiet=True)
    if path and os.path.isdir(path):
        if hasattr(os, "startfile"):
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])
    else:
        nuke.message("No cache folder yet:\n%s" % path)


def refresh_versions(grp, select=None):
    cache = _node_cache_dir(grp, quiet=True)
    versions = core.good_versions(cache) if cache else []
    current = grp["version"].value()
    grp["version"].setValues(versions or ["none"])
    target = select or (current if current in versions else (versions[-1] if versions else "none"))
    grp["version"].setValue(target)
    apply_version(grp, target)


def apply_version(grp, version):
    """Point the internal Reads at one version folder, or show passthrough."""
    cache = _node_cache_dir(grp, quiet=True)
    run_dir = os.path.join(cache, version) if (cache and version not in ("", "none")) else None
    manifest = core.read_manifest(run_dir) if run_dir else {}
    if manifest.get("status") != "ok":
        grp["has_result"].setValue(False)
        return

    first = int(manifest["first_frame"])
    grp.begin()
    try:
        for read_name, output in (("Read_fg", "fg"), ("Read_cutout", "cutout")):
            n = core.count_output_frames(run_dir, output)
            read = nuke.toNode(read_name)
            # fromUserText sets the format and range from the files on disk.
            read["file"].fromUserText("%s 0-%d" % (core.output_pattern(run_dir, output), max(n - 1, 0)))
            read["frame_mode"].setValue("start at")
            read["frame"].setValue(str(first))
            read["raw"].setValue(True)
    finally:
        grp.end()
    grp["has_result"].setValue(True)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run(grp):
    full_name = grp.fullName()
    if full_name in _RUNNING:
        nuke.message("This node is already keying. Wait or cancel it in the progress bar.")
        return

    engine_root, python, err = core.find_engine()
    if err:
        nuke.message("CorridorKey2 engine not available on this machine:\n\n" + err)
        return
    if grp.input(0) is None:
        nuke.message("Connect a plate to the Source input.")
        return

    hint_mode = HINT_MODE_MAP[grp["hint_mode"].value()]
    if hint_mode != "none" and grp.input(1) is None:
        nuke.message("Hint is set to '%s' but nothing is connected to the Hint input."
                     % grp["hint_mode"].value())
        return

    cache = _node_cache_dir(grp)
    if not cache:
        return
    first, last = int(grp["frame_first"].value()), int(grp["frame_last"].value())
    if last < first:
        nuke.message("Frame range is empty.")
        return

    version = core.next_version(cache)
    run_dir = os.path.join(cache, version)
    core.make_run_dirs(run_dir, with_hint=hint_mode != "none")

    opts = {
        "fps": nuke.root()["fps"].value(),
        "hint_mode": hint_mode,
        "hint_quality": grp["hint_quality"].value(),
        "despill": grp["despill"].value(),
        "low_vram": bool(grp["low_vram"].value()),
        "exr_dtype": grp["exr_dtype"].value(),
        "temp_dir": grp["temp_dir"].evaluate() if grp["temp_dir"].value() else "",
    }
    cmd = core.build_infer_command(python, engine_root, run_dir, opts)
    manifest = core.new_manifest(version, first, last, opts, cmd, engine_root)
    core.write_manifest(run_dir, manifest)

    # Step 1 (main thread): render plate and hint frames with Nuke itself.
    try:
        _render_inputs(grp, run_dir, first, last, hint_mode, opts["exr_dtype"])
    except Exception as e:  # noqa: broad on purpose, reported to the artist
        manifest["status"] = "failed"
        manifest["error"] = "render inputs: %s" % e
        core.write_manifest(run_dir, manifest)
        nuke.message("CorridorKey2: rendering the input failed.\n\n%s" % e)
        return

    # Step 2 (background thread): run the engine.
    _RUNNING.add(full_name)
    t = threading.Thread(target=_engine_worker,
                         args=(full_name, run_dir, version, cmd, engine_root))
    t.daemon = True
    t.start()


def _render_inputs(grp, run_dir, first, last, hint_mode, exr_dtype):
    """
    Build temporary nodes inside the Group, execute them, delete them.
    Nothing persistent means the saved script never contains Write nodes
    that a farm "render all" could pick up.
    """
    temp = []
    grp.begin()
    try:
        src = nuke.toNode("Source")
        fmt = grp.input(0).format()
        crop = _temp(temp, nuke.nodes.Crop(inputs=[src]))
        _crop_to_format(crop, fmt)
        w_plate = _temp(temp, nuke.nodes.Write(inputs=[crop]))
        _setup_write(w_plate, core.plate_pattern(run_dir), "rgb", exr_dtype)
        nuke.execute(w_plate, first, last, 1)

        if hint_mode != "none":
            hint_in = nuke.toNode("Hint")
            expr = _temp(temp, nuke.nodes.Expression(inputs=[hint_in]))
            channel = "a" if grp["hint_channel"].value() == "alpha" else "r"
            expr["channel0"].setValue("rgba")
            expr["expr0"].setValue(channel)
            for i in (1, 2, 3):
                expr["channel%d" % i].setValue("none")
            hcrop = _temp(temp, nuke.nodes.Crop(inputs=[expr]))
            _crop_to_format(hcrop, fmt)
            w_hint = _temp(temp, nuke.nodes.Write(inputs=[hcrop]))
            _setup_write(w_hint, core.hint_pattern(run_dir), "rgba", "half")
            hint_last = first if hint_mode == "first" else last
            nuke.execute(w_hint, first, hint_last, 1)
    finally:
        for n in reversed(temp):
            nuke.delete(n)
        grp.end()


def _temp(temp_list, node):
    temp_list.append(node)
    return node


def _crop_to_format(crop, fmt):
    """
    Clamp the image to the format so the EXR data window equals the frame.
    Every knob is set explicitly because the studio init.py changes the Crop
    defaults (reformat on), which would otherwise leak into this node.
    """
    crop["box"].setValue([0, 0, fmt.width(), fmt.height()])
    crop["reformat"].setValue(False)
    crop["intersect"].setValue(False)
    crop["crop"].setValue(True)


def _setup_write(write, path, channels, exr_dtype):
    """
    Same idea as the Crop: studio defaults set DWAB compression and autocrop
    on Write nodes. DWA is lossy and autocrop shrinks the data window, both
    wrong for an ML input, so override them here.
    """
    write["file"].setValue(path)
    write["file_type"].setValue("exr")
    write["channels"].setValue(channels)
    write["raw"].setValue(True)
    write["datatype"].setValue("32 bit float" if exr_dtype == "float" else "16 bit half")
    write["compression"].setValue("Zip (1 scanline)")
    write["autocrop"].setValue(False)
    write["create_directories"].setValue(True)


def _engine_worker(full_name, run_dir, version, cmd, engine_root):
    """Runs in a background thread. Never touches nodes directly."""
    task = nuke.ProgressTask("CorridorKey2  %s  %s" % (full_name, version))
    task.setMessage("loading model...")
    log_path = os.path.join(run_dir, core.ENGINE_LOG)
    code, cancelled, recent = -1, False, ""
    try:
        with open(log_path, "wb") as log:
            log.write((" ".join(cmd) + "\n\n").encode("utf-8"))
            proc = subprocess.Popen(cmd, cwd=engine_root, env=core.child_env(),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    **core.popen_kwargs())
            while True:
                chunk = proc.stdout.read1(4096)
                if not chunk:
                    break
                log.write(chunk)
                text = chunk.decode("utf-8", errors="replace")
                recent = (recent + text)[-4000:]
                pct = core.parse_progress(text)
                if pct is not None:
                    task.setProgress(pct)
                    task.setMessage("keying %d%%" % pct)
                if task.isCancelled() and not cancelled:
                    cancelled = True
                    proc.terminate()
            code = proc.wait()
    except Exception as e:  # noqa: reported to the artist on the main thread
        recent += "\n%s" % e
    finally:
        del task
        nuke.executeInMainThread(_finish, args=(full_name, run_dir, version,
                                                code, cancelled, core.tail(recent)))


def _finish(full_name, run_dir, version, code, cancelled, log_tail):
    """Main thread. Records the outcome and, on success, loads the result."""
    _RUNNING.discard(full_name)
    manifest = core.read_manifest(run_dir)
    ok = code == 0 and not cancelled and core.count_output_frames(run_dir, "cutout") > 0
    manifest["status"] = "ok" if ok else ("cancelled" if cancelled else "failed")
    manifest["exit_code"] = code
    core.write_manifest(run_dir, manifest)

    grp = nuke.toNode(full_name)
    if grp is None:
        return  # node was deleted or renamed while running; result stays on disk
    if ok:
        refresh_versions(grp, select=version)
    elif not cancelled:
        nuke.message("CorridorKey2 failed (exit code %s).\nFull log: %s\n\n%s"
                     % (code, os.path.join(run_dir, core.ENGINE_LOG), log_tail))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _node_cache_dir(grp, quiet=False):
    if not nuke.root().name() or nuke.root().name() == "Root":
        if not quiet:
            nuke.message("Save the script first. Results are cached next to it.")
        return None
    root = grp["cache_root"].evaluate()
    if not root:
        return None
    return os.path.normpath(os.path.join(root, grp.name()))


def _set_status(grp):
    engine_root, _, err = core.find_engine()
    if err:
        grp["status"].setValue("<font color=orange>not installed here</font>  (results still render)")
        return
    ver = core.read_engine_version(engine_root)
    grp["status"].setValue("ok  %s" % (ver.get("release") or engine_root))
