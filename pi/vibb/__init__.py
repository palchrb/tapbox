"""vibb — shared modules for the Vibb daemons and CLIs.

One import path for the helpers that used to be copy-pasted between the
entry scripts (daemon/player/buttons/idle/rfid/ui):

  vibb.spotify  go-librespot client (API, link parsing, smart prev)
  vibb.mpv      mpv IPC socket client
  vibb.boxapi   vibbd HTTP client (the :3679 API)
  vibb.paths    state/cache/settings locations (env-overridable)
  vibb.content  NRK/RSS/folder link expansion + episode cache (ex nrk.py)

The package lives next to the entry scripts in the repo (pi/vibb/) and
is installed to /usr/local/lib/vibb-py/vibb on the box; each entry
script bootstraps exactly one of those onto sys.path (repo wins), so the
stale-copy import bugs of the loose-script era cannot recur.
"""

__version__ = "0.1"
