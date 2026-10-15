# Gunicorn settings that can't be passed on the command line. start.sh loads this with -c.
import os
import sys


# A worker that is recycled (--max-requests) or stopped waits --graceful-timeout for its
# requests, then logs "Worker exiting". Python's normal shutdown after that still waits
# for every request thread to finish, however long that takes. With one worker nothing is
# answering in the meantime, so a single slow request (an LLM call, an email send) left
# the service failing health checks until Render restarted it. Ending the process here
# lets the arbiter start the replacement worker straight away. Only after boot: a worker
# that failed to boot keeps gunicorn's own exit code, which tells the arbiter to stop.
def worker_exit(server, worker):
    if getattr(worker, "booted", False):
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
