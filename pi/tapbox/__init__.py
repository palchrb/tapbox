"""tapbox — shared modules for the TapBox daemons and CLIs.

One import path for the helpers that used to be copy-pasted between the
entry scripts (daemon/player/buttons/idle/rfid/ui):

  tapbox.spotify  go-librespot client (API, link parsing, smart prev)
  tapbox.mpv      mpv IPC socket client
  tapbox.boxapi   tapboxd HTTP client (the :3679 API)
  tapbox.paths    state/cache/settings locations (env-overridable)
  tapbox.content  NRK/RSS/folder link expansion + episode cache (ex nrk.py)

The package lives next to the entry scripts in the repo (pi/tapbox/) and
is installed to /usr/local/lib/tapbox-py/tapbox on the box; each entry
script bootstraps exactly one of those onto sys.path (repo wins), so the
stale-copy import bugs of the loose-script era cannot recur.
"""

__version__ = "0.1"
