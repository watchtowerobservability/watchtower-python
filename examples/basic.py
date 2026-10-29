import os
import watchtower

watchtower.init(os.environ["WT_INGEST_KEY"], service_name="example-python")


@watchtower.observe("hello")
def hello():
    return "hello"


hello()
watchtower.shutdown()  # flush before exit
