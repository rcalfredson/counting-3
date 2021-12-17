import argparse
import logging
import os
import time
import waitress


from project.lib.web.downloadManager import DownloadManager
from project.lib.web.gpu_manager import GPUManager
from project.lib.web.scheduler import Scheduler
from project.routes import socket_events

from . import create_app


UPLOAD_FOLDER = "./uploads"


def prune_old_sessions():
    current_time = time.time()
    for sid in list(app.sessions.keys()):
        if current_time - app.sessions[sid].lastPing > 60 * 10:
            del app.sessions[sid]


flask_env = os.environ["FLASK_ENV"]
app = create_app()
app.sessions = {}
app.downloadManager = DownloadManager()
app.gpu_manager = GPUManager()
socket_events.setup_event_handlers()
scheduler = Scheduler(1)
scheduler.schedule.every(5).minutes.do(prune_old_sessions)
scheduler.run_continuously()
if flask_env == 'production':
    p = argparse.ArgumentParser(description="run the egg-counting server")
    p.add_argument(
        "--host", default="127.0.0.1", help="address where the server should run"
    )
    p.add_argument(
        "--port", default="5000", help="port where the server should listen"
    )
    opts = p.parse_args()
    logger = logging.getLogger('waitress')
    logger.setLevel(logging.INFO)
    waitress.serve(app, host=opts.host, port=opts.port)
    
