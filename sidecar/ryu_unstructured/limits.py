"""Resource limits for one parse, fixed in code.

These are the *contract* floors, not tuning knobs: Core sizes its own timeouts
against them, and the ext-proxy caps a forwarded body at 10 MiB independently, so
the output cap here must stay below that or a large result becomes unreadable
rather than truncated. They are not read from the environment: every node
runs the same bounds, and /capability reports them.
"""

from __future__ import annotations

_MIB = 1024 * 1024

# Largest input file (or archive) we will open. Inputs arrive as a path to a
# Core-owned blob, not as an upload, so this is a disk-read cap rather than a
# request-body cap.
MAX_INPUT_BYTES = 200 * _MIB

# Largest markdown+text payload a job result may carry. Beyond this the result is
# truncated and flagged (`truncated: true`) rather than dropped: a clipped
# document is useful, a 500 is not.
MAX_OUTPUT_BYTES = 8 * _MIB

# Wall-clock ceiling for one parse. OCR over a scanned 300-page PDF genuinely
# takes minutes, hence the generous default.
TIMEOUT_SECS = 600

# Concurrent parses. `unstructured` is CPU-bound and its hi_res path loads a
# detection model per process, so a small number is the honest default.
MAX_WORKERS = 2

# Members we will expand out of one archive, and total expanded bytes — a zip
# bomb is otherwise a trivial local DoS.
MAX_ARCHIVE_MEMBERS = 512
MAX_ARCHIVE_BYTES = 512 * _MIB

# Finished jobs kept in the table before the oldest are evicted. The result of a
# parse is large; an unbounded table is a slow memory leak in a process that is
# meant to idle-stop.
MAX_JOBS = 64
