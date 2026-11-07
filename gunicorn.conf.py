# Gunicorn config — authoritative, auto-loaded when started with `-c gunicorn.conf.py`.
#
# EXACTLY ONE worker is required: this app keeps all live-match state in memory,
# so multiple workers would each hold a separate, inconsistent store (and each run
# its own poller — duplicate API calls). One worker + threads handles the light
# web traffic fine.
workers = 1
threads = 8
timeout = 120
worker_class = "gthread"


def post_fork(server, worker):
    # Start the background poller inside the worker, AFTER the fork, so the thread
    # lives in the process that actually serves requests. (Threads don't survive
    # gunicorn's fork, so starting at import time would leave the worker with none.)
    import app
    app.start_poller()
