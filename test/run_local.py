"""本地单测启动器（Windows 开发机，无 AstrBot 容器时使用）。

用 test/_local 下的最小 astrbot 桩模块满足插件的 import 面，加载
test_oil_plugin.py 跑全量用例；桩只覆盖 import 面，逻辑均为真实实现。

用法：python test/run_local.py
真实环境仍以容器内运行为准：
    cd /AstrBot/data/plugins && python3 -m unittest astrbot_plugin_oil_price.test.test_oil_plugin -v
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "_local"))  # astrbot 桩
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))  # 插件仓库的父目录

spec = importlib.util.spec_from_file_location(
    "test_oil_plugin_local", os.path.join(HERE, "test_oil_plugin.py")
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Windows 无 /tmp：把测试根指到系统临时目录
TEST_ROOT = os.path.join(tempfile.gettempdir(), "oil_plugin_test_local")
shutil.rmtree(TEST_ROOT, ignore_errors=True)
os.makedirs(TEST_ROOT, exist_ok=True)
mod.TEST_ROOT = TEST_ROOT

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(mod)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(
        f"\nran={result.testsRun} failures={len(result.failures)} errors={len(result.errors)}"
    )
    sys.exit(0 if result.wasSuccessful() else 1)
