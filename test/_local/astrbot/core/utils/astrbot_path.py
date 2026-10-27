"""get_astrbot_data_path 桩：返回本地可写目录。"""

import os
import tempfile


def get_astrbot_data_path():
    return os.environ.get("OIL_TEST_DATA_ROOT") or os.path.join(
        tempfile.gettempdir(), "astrbot_data"
    )
