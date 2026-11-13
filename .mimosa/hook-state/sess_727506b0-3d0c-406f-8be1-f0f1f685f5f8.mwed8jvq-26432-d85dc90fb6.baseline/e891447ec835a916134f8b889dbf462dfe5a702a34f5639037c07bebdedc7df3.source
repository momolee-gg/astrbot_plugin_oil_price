"""astrbot.api 桩：logger。"""

import logging

logger = logging.getLogger("astrbot")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(
        logging.Formatter("[%(levelname)s %(asctime)s] %(message)s", "%H:%M:%S")
    )
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
