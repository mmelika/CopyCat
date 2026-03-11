from __future__ import annotations

import signal
import threading
import time

from copytrader import database
from copytrader.config import DB_PATH
from copytrader.engine import CopyTradingEngine


def main() -> None:
    database.init_db(DB_PATH)
    engine = CopyTradingEngine(DB_PATH)
    stop_event = threading.Event()

    def handle_signal(signum, _frame) -> None:
        database.log("INFO", "engine", f"Engine runner received signal {signum}, stopping.", db_path=DB_PATH)
        stop_event.set()
        engine.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    database.log("INFO", "engine", "Engine runner starting.", db_path=DB_PATH)
    engine.start()
    try:
        while not stop_event.is_set():
            time.sleep(1)
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
