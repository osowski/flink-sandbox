import logging
import os

from dtm.simulator import runner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
runner.run(os.environ)
