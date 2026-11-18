# CorridorKey2 node for Nuke (MAKE Visual)

A Python Group node that runs the CorridorKey 2.0 engine from this repo. No
compiled code, so it works across Nuke versions without rebuilding.

## Files

| File | Purpose |
|---|---|
| `corridorkey2_core.py` | Logic with no Nuke dependency: engine discovery, command line, version folders, manifests. Testable outside Nuke. |
| `corridorkey2_nuke.py` | The node: knobs, internal graph, rendering inputs, running the engine in a background thread. |
| `menu.py` | Adds `MAKE/CorridorKey2` and `Keyer/CorridorKey2` to the Nodes toolbar. Runs automatically when this folder is on the plugin path. |

## Artist workflow

1. Save the script (results are cached next to it by default).
2. Create `CorridorKey2`, connect the plate to **Source**. Optionally connect a rough matte to **Hint** and pick a hint mode.
3. Press **Key Shot**. A progress bar appears; the UI stays usable. Cancel from the progress bar.
4. On success the node switches to the new version. Use **version** to roll back.

Output is linear RGBA, premultiplied by default. Either:
- **despilled (engine)**: the engine's despill, strength set before the run, or
- **raw fg + alpha**: un-despilled foreground, for doing the despill in Nuke.

## Machine requirements

- Pressing **Key Shot**: NVIDIA RTX 30-series or newer, release installed with `deploy/Install-CK2.ps1`, `MAKE_CK2_ROOT` set.
- Rendering a script that contains the node: nothing. The saved node is just Reads of the cached EXRs.

## Cache layout

See the docstring at the top of `corridorkey2_core.py`. Every run is a new
`vNNN` folder with a `manifest.json` (settings, exact command, engine release,
user, host) and the full `engine.log`.
