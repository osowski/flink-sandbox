"""Run the UI service: ``python -m dtm.ui``, or ``python -m dtm.ui --fake`` for synthetic data."""
import argparse
import logging
import os

import httpx
import uvicorn

from dtm.ui.app import build_app
from dtm.ui.fake import FakeFeed
from dtm.ui.hub import Hub
from dtm.ui.model import Board
from dtm.ui.sources import ContextPoller, TopicReader, context_base_url


def board_health(board: Board) -> dict:
    """Report how long each source has been stale; the probe itself never fails on upstream trouble."""
    stale = board.snapshot()["stale"]
    return {"kafka_stale_since_ms": stale["kafka"], "context_stale_since_ms": stale["context"]}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m dtm.ui", description=__doc__)
    parser.add_argument("--fake", action="store_true", help="feed the board synthetic data instead of Kafka")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    board = Board()
    hub = Hub(board)
    if args.fake:
        workers = [FakeFeed(board, hub.publish_threadsafe)]
        http = None
    else:
        http = httpx.Client(timeout=2.0)
        workers = [TopicReader.connect(board, hub.publish_threadsafe),
                   ContextPoller(board, http, hub.publish_threadsafe, context_base_url())]

    app = build_app(hub, lambda: board_health(board), message=board.message, message_limit=board.feed_limit)
    for worker in workers:
        worker.start()
    try:
        uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("DTM_UI_PORT", 8080)), log_level="info")
    finally:
        for worker in workers:
            worker.stop()
        if http is not None:
            http.close()


if __name__ == "__main__":
    main()
