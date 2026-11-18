# menu.py - runs automatically when this folder is on Nuke's plugin path.
# Registers the CorridorKey2 node. Kept here, next to the node code, so the
# studio Nuke repo only needs one pluginAddPath line to pick it up.

import nuke

_CMD = "import corridorkey2_nuke; corridorkey2_nuke.create()"

nuke.menu("Nodes").addCommand("MAKE/CorridorKey2", _CMD)
nuke.menu("Nodes").addCommand("Keyer/CorridorKey2", _CMD)
