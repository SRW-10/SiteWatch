import ipaddress
import socket
import sqlite3
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

import requests
from flask import Flask, flash, redirect, render_template, request, url_for

app = Flask(__name__)
app.secret_key = "sitewatch-college-project"
DB_NAME = "sitewatch.db"
REQUEST_TIMEOUT = 5
WORKER_STARTED = False
WORKER_LOCK = threading.Lock()


def get_db():
    connection = sqlite3.connect(DB_NAME)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db():
    connection = get_db()
    connection.execute("""
        CREATE TABLE IF NOT EXISTS Websites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            interval_seconds INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS MonitoringHistory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            website_id INTEGER NOT NULL,
            status_code INTEGER,
            response_time INTEGER NOT NULL,
            result TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            error_message TEXT,
            FOREIGN KEY (website_id) REFERENCES Websites(id) ON DELETE CASCADE
        )
    """)
    connection.commit()
    connection.close()


def is_blocked_ip(address):
    try:
        ip = ipaddress.ip_address(address)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        return True


def validate_url(url):
    if not isinstance(url, str):
        return False, "Please enter a website URL."
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, "Only HTTP and HTTPS URLs are allowed."
    if not parsed.netloc or parsed.username or parsed.password:
        return False, "Enter a complete and valid URL."
    if parsed.hostname is None:
        return False, "Enter a valid website hostname."

    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return False, "Local and internal destinations are not allowed."

    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, None)}
    except socket.gaierror:
        return False, "The website hostname could not be resolved."

    if not addresses:
        return False, "The website hostname could not be resolved."
    if any(is_blocked_ip(address) for address in addresses):
        return False, "Private, local, and internal destinations are not allowed."
    return True, url


def save_check(website_id, url):
    global REQUEST_TIMEOUT
    start = time.perf_counter()
    status_code = None
    error_message = None
    try:
        valid, message = validate_url(url)
        if not valid:
            raise requests.exceptions.RequestException(message)

        response = requests.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=False)
        status_code = response.status_code
        if 200 <= status_code <= 399:
            result = "Online"
        elif 400 <= status_code <= 599:
            result = "Error"
            error_message = "HTTP error response received from the server."
        else:
            result = "Error"
            error_message = "Unexpected HTTP status response."
    except requests.exceptions.Timeout:
        result = "Down"
        error_message = "The website request timed out."
    except requests.exceptions.ConnectionError as error:
        result = "Down"
        if "hostname" in str(error).lower() or "name or service" in str(error).lower():
            error_message = "The website hostname could not be resolved."
        else:
            error_message = "Could not connect to the website."
    except requests.exceptions.RequestException as error:
        result = "Down"
        error_message = str(error).split(":", 1)[0][:200]
    except Exception:
        result = "Down"
        error_message = "The website request failed unexpectedly."

    elapsed_ms = int((time.perf_counter() - start) * 1000)
    connection = get_db()
    connection.execute("""
        INSERT INTO MonitoringHistory
        (website_id, status_code, response_time, result, checked_at, error_message)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (website_id, status_code, elapsed_ms, result, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), error_message))
    connection.commit()
    connection.close()
    return result


def get_latest_records():
    connection = get_db()
    rows = connection.execute("""
        SELECT w.id, w.url, w.interval_seconds, h.status_code, h.response_time,
               h.result, h.checked_at
        FROM Websites w
        LEFT JOIN MonitoringHistory h ON h.id = (
            SELECT mh.id FROM MonitoringHistory mh
            WHERE mh.website_id = w.id
            ORDER BY mh.checked_at DESC, mh.id DESC LIMIT 1
        )
        ORDER BY w.id DESC
    """).fetchall()
    connection.close()
    return rows


def get_summaries():
    records = get_latest_records()
    total = len(records)
    online = sum(1 for row in records if row["result"] == "Online")
    failing = sum(1 for row in records if row["result"] in ("Down", "Error") )
    response_times = [row["response_time"] for row in records if row["response_time"] is not None]
    average = int(sum(response_times) / len(response_times)) if response_times else 0
    return total, online, failing, average


@app.route("/")
def dashboard():
    records = get_latest_records()
    summaries = get_summaries()
    return render_template("dashboard.html", records=records, summaries=summaries)


@app.route("/add", methods=["GET", "POST"])
def add_website():
    if request.method == "POST":
        url = request.form.get("url", "").strip()
        interval = request.form.get("interval", "30")
        if interval not in ("30", "60"):
            interval = "30"
        valid, message = validate_url(url)
        if not valid:
            flash(message, "error")
        else:
            connection = get_db()
            existing = connection.execute("SELECT id FROM Websites WHERE url = ?", (url,)).fetchone()
            if existing:
                connection.close()
                flash("This website has already been added.", "error")
            else:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cursor = connection.execute(
                    "INSERT INTO Websites (url, interval_seconds, created_at) VALUES (?, ?, ?)",
                    (url, int(interval), now)
                )
                connection.commit()
                website_id = cursor.lastrowid
                connection.close()
                save_check(website_id, url)
                flash("Website added and checked successfully.", "success")
                return redirect(url_for("dashboard"))
    return render_template("add.html")


@app.route("/check/<int:website_id>", methods=["POST"])
def check_now(website_id):
    connection = get_db()
    website = connection.execute("SELECT url FROM Websites WHERE id = ?", (website_id,)).fetchone()
    connection.close()
    if website:
        save_check(website_id, website["url"])
        flash("Website check completed.", "success")
    else:
        flash("Website was not found.", "error")
    return redirect(url_for("dashboard"))


@app.route("/remove/<int:website_id>", methods=["POST"])
def remove_website(website_id):
    connection = get_db()
    connection.execute("DELETE FROM Websites WHERE id = ?", (website_id,))
    connection.commit()
    connection.close()
    flash("Website removed.", "success")
    return redirect(url_for("dashboard"))


@app.route("/history")
def history():
    website_id = request.args.get("website_id", "")
    connection = get_db()
    websites = connection.execute("SELECT id, url FROM Websites ORDER BY url").fetchall()
    if website_id:
        records = connection.execute("""
            SELECT h.*, w.url FROM MonitoringHistory h
            JOIN Websites w ON w.id = h.website_id
            WHERE h.website_id = ? ORDER BY h.checked_at DESC, h.id DESC
        """, (website_id,)).fetchall()
    else:
        records = connection.execute("""
            SELECT h.*, w.url FROM MonitoringHistory h
            JOIN Websites w ON w.id = h.website_id
            ORDER BY h.checked_at DESC, h.id DESC
        """).fetchall()
    connection.close()
    return render_template("history.html", records=records, websites=websites, selected=website_id)


@app.route("/clear-history", methods=["POST"])
def clear_history():
    connection = get_db()
    connection.execute("DELETE FROM MonitoringHistory")
    connection.commit()
    connection.close()
    flash("Monitoring history cleared. Website list was kept.", "success")
    return redirect(url_for("history"))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    global REQUEST_TIMEOUT
    if request.method == "POST":
        timeout = request.form.get("timeout", "5")
        if timeout not in ("3", "5", "10"):
            flash("Choose a valid timeout.", "error")
        else:
            REQUEST_TIMEOUT = int(timeout)
            flash("Request timeout updated for this running application.", "success")
    return render_template("settings.html", timeout=REQUEST_TIMEOUT)


def monitoring_worker():
    last_checked = {}
    while True:
        time.sleep(1)
        now = time.time()
        connection = get_db()
        websites = connection.execute("SELECT id, url, interval_seconds FROM Websites").fetchall()
        connection.close()
        for website in websites:
            website_id = website["id"]
            elapsed = now - last_checked.get(website_id, 0)
            if elapsed >= website["interval_seconds"]:
                save_check(website_id, website["url"])
                last_checked[website_id] = now


def start_monitoring_worker():
    global WORKER_STARTED
    with WORKER_LOCK:
        if not WORKER_STARTED:
            WORKER_STARTED = True
            worker = threading.Thread(target=monitoring_worker, name="SiteWatchMonitor", daemon=True)
            worker.start()


init_db()
if __name__ == "__main__":
    start_monitoring_worker()
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
