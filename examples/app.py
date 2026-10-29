"""Minimal app: `WT_INGEST_KEY=... python examples/app.py` sends one trace, one handled and one logged error."""
import logging

import watchtower

watchtower.init(service_name="example-python", buffer_dir=".wt-buffer")  # key from WT_INGEST_KEY
logging.getLogger().addHandler(watchtower.WatchtowerHandler())


@watchtower.observe("checkout")
def checkout():
    try:
        1 / 0
    except ZeroDivisionError as e:
        print("trace id:", watchtower.capture_exception(e))


checkout()
watchtower.shutdown()
