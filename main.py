import logging
import threading
import time

import dashboard
from bot import TradingBot

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)

logging.getLogger("werkzeug").setLevel(logging.WARNING)

log = logging.getLogger(__name__)


def _start_dashboard():
    dashboard.app.run(host="0.0.0.0", port=8080, use_reloader=False)


def _run_bot():
    try:
        TradingBot().run()
    except Exception as e:
        log.error(f"Bot crashed: {e}", exc_info=True)


if __name__ == "__main__":
    flask_thread = threading.Thread(target=_start_dashboard, daemon=False)
    flask_thread.start()
    log.info("Dashboard running at http://localhost:8080")

    bot_thread = threading.Thread(target=_run_bot, daemon=True)
    bot_thread.start()

    flask_thread.join()
