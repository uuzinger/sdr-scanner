#!/usr/bin/env python3
"""
SDR Scanner Web Dashboard
Serves the same info as sdr_gui.py (system health, recent calls, traffic
histogram, live log stream) over HTTP so it can be viewed from any browser
on the network instead of only on the attached monitor.

Run:
    python3 app.py
Then browse to http://<host>:8080/  (port configurable below / via env var)
"""

import os
import re
import glob
import time
import threading
from collections import deque

import psutil
from flask import Flask, jsonify, render_template

# --- Configuration ---
LOG_DIR = os.environ.get("SDR_LOG_DIR", "/home/zinger/trunk-build/logs")
HOST = os.environ.get("SDR_WEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("SDR_WEB_PORT", "8080"))
POLL_INTERVAL = 0.2   # how often the background thread checks for new log lines
BUCKET_SECONDS = 2.0  # width of each histogram bucket (60 buckets = 2 min window)
CPU_SAMPLE_SECONDS = 1.0

# --- Shared State (guarded by _lock) ---
_lock = threading.Lock()
state = {
    "cpu": 0.0,
    "mem": 0.0,
    "net_rx_kbps": 0.0,
    "net_tx_kbps": 0.0,
    "cpu_history": deque([0] * 60, maxlen=60),
    "msg_history": deque([0] * 60, maxlen=60),
    "recent_calls": deque(maxlen=6),
    "raw_logs": deque(maxlen=15),
    "current_log_path": None,
}


def get_latest_log():
    list_of_files = glob.glob(f"{LOG_DIR}/*.log")
    if not list_of_files:
        return None
    return max(list_of_files, key=os.path.getctime)


def parse_trunk_recorder_line(line):
    log_pattern = re.compile(
        r'\[(.*?)\]\s+\((.*?)\)\s+\[(.*?)\]\s+([\w]+)\s+TG:\s+(\d+)\s+Freq:\s+([\d.]+)\s+MHz\s+(?:-\s+)?(.*)'
    )
    match = log_pattern.search(line)
    if not match:
        return None

    data = {
        "tg": match.group(5),
        "freq": match.group(6),
        "event": match.group(7).strip(),
    }
    alias_match = re.search(r'\((.*?)\)', data["event"])
    data["alias"] = (
        alias_match.group(1)
        if alias_match and ("src:" in data["event"] or "alias:" in data["event"])
        else ""
    )
    return data


def background_worker():
    """Tails the current trunk-recorder log and updates shared state,
    mirroring the update loop in sdr_gui.py."""
    net_start = psutil.net_io_counters()
    last_bucket_time = time.time()
    last_cpu_check = time.time()
    last_log_check = time.time()

    current_log_path = get_latest_log()
    log_file = open(current_log_path, "r") if current_log_path else None
    if log_file:
        log_file.seek(0, 2)  # start at end, like the gui version

    with _lock:
        state["current_log_path"] = current_log_path

    while True:
        current_time = time.time()

        # Roll the histogram forward
        with _lock:
            while current_time - last_bucket_time >= BUCKET_SECONDS:
                state["msg_history"].append(0)
                last_bucket_time += BUCKET_SECONDS

        # CPU / mem / net sampling
        if current_time - last_cpu_check >= CPU_SAMPLE_SECONDS:
            cpu = psutil.cpu_percent()
            mem = psutil.virtual_memory().percent
            net_now = psutil.net_io_counters()
            rx = (net_now.bytes_recv - net_start.bytes_recv) / 1024 / CPU_SAMPLE_SECONDS
            tx = (net_now.bytes_sent - net_start.bytes_sent) / 1024 / CPU_SAMPLE_SECONDS
            net_start = net_now
            last_cpu_check = current_time

            with _lock:
                state["cpu"] = cpu
                state["mem"] = mem
                state["net_rx_kbps"] = rx
                state["net_tx_kbps"] = tx
                state["cpu_history"].append(cpu)

        # New log lines
        if log_file:
            line = log_file.readline()
            if line:
                clean_line = line.strip()
                parsed = parse_trunk_recorder_line(clean_line)
                with _lock:
                    state["raw_logs"].appendleft(clean_line)
                    state["msg_history"][-1] += 1
                    if parsed and parsed["alias"]:
                        state["recent_calls"].appendleft(parsed)
            else:
                if current_time - last_log_check > 3:
                    last_log_check = current_time
                    newest_log = get_latest_log()
                    if newest_log and newest_log != current_log_path:
                        current_log_path = newest_log
                        log_file.close()
                        log_file = open(current_log_path, "r")
                        with _lock:
                            state["current_log_path"] = current_log_path
        else:
            # No log file was found yet at startup; keep checking for one
            if current_time - last_log_check > 3:
                last_log_check = current_time
                newest_log = get_latest_log()
                if newest_log:
                    current_log_path = newest_log
                    log_file = open(current_log_path, "r")
                    log_file.seek(0, 2)
                    with _lock:
                        state["current_log_path"] = current_log_path

        time.sleep(POLL_INTERVAL)


app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    with _lock:
        snapshot = {
            "cpu": round(state["cpu"], 1),
            "mem": round(state["mem"], 1),
            "net_rx_kbps": round(state["net_rx_kbps"], 1),
            "net_tx_kbps": round(state["net_tx_kbps"], 1),
            "cpu_history": list(state["cpu_history"]),
            "msg_history": list(state["msg_history"]),
            "recent_calls": list(state["recent_calls"]),
            "raw_logs": list(state["raw_logs"]),
            "current_log_path": state["current_log_path"],
        }
    return jsonify(snapshot)


if __name__ == "__main__":
    worker = threading.Thread(target=background_worker, daemon=True)
    worker.start()
    app.run(host=HOST, port=PORT, threaded=True)
