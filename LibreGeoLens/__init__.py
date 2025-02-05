import sys
import os

plugin_dir = os.path.dirname(__file__)
libs_path = os.path.join(plugin_dir, "libs")

if os.path.exists(libs_path) and libs_path not in sys.path:
    sys.path.insert(0, libs_path)


def classFactory(iface):  # pylint: disable=invalid-name
    from .libre_geo_lens import LibreGeoLens
    return LibreGeoLens(iface)
