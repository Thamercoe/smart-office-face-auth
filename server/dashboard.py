from __future__ import annotations

import logging
from typing import Any

from flask import Flask, jsonify, render_template
from werkzeug.serving import WSGIRequestHandler, make_server


class QuietRequestHandler(WSGIRequestHandler):
    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        pass

    def log_error(self, format: str, *args: Any) -> None:
        pass

    def log_message(self, format: str, *args: Any) -> None:
        pass


def create_dashboard_app(server: Any) -> Flask:
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )

    app.logger.disabled = True
    logging.getLogger("werkzeug").disabled = True

    @app.route("/")
    def index():
        return render_template("dashboard.html")

    @app.route("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.route("/api/state")
    def api_state():
        return jsonify(server.get_dashboard_state())

    return app


def run_dashboard(server: Any, host: str = "127.0.0.1", port: int = 5055) -> None:
    app = create_dashboard_app(server)

    http_server = make_server(
        host=host,
        port=port,
        app=app,
        threaded=True,
        request_handler=QuietRequestHandler,
    )

    print(f"Dashboard: http://{host}:{port}")

    http_server.serve_forever()
