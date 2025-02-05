# From https://github.com/ivanlonel/qgis-plugin-with-pip-dependencies/blob/master/__init__.py in order to try to install
# python dependencies automatically when the plugin is first loaded

import subprocess
from pathlib import Path


def setup_user_site() -> tuple[str, Path, dict[str, str]]:
    import os
    import sys

    from qgis.core import QgsApplication
    from qgis.PyQt.QtCore import QStandardPaths

    python = QStandardPaths.findExecutable("python")

    env = os.environ.copy()
    env.setdefault(
        "PYTHONUSERBASE",
        str(Path(QgsApplication.qgisSettingsDirPath()) / "python"),
    )

    # get exact location of the usersite (for the current QGIS interpreter version)
    site_packages = Path(
        subprocess.run(
            (python, "-m", "site", "--user-site"),
            env=env,
            shell=True,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .split()[0]
    )

    # raise exception if it's not a subpath
    site_packages.relative_to(env["PYTHONUSERBASE"])

    # set the priority to our custom user site
    site_packages.mkdir(parents=True, exist_ok=True)
    sys.path.insert(1, str(site_packages))

    return python, site_packages, env


def install_dependencies(python: str, site_packages: Path, env: dict[str, str]) -> None:
    from configparser import ConfigParser

    plugin_dir = Path(__file__).parent

    requirements_txt = plugin_dir / "requirements.txt"
    if not requirements_txt.is_file():
        return

    config = ConfigParser(allow_no_value=True)
    config.read(plugin_dir / "metadata.txt")
    metadata = dict(config["general"])

    log_file = site_packages.parent / f"{metadata['name']}.log"
    with log_file.open("a") as output:
        subprocess.run(
            (python, "-m", "pip", "install", "--user", "-r", str(requirements_txt)),
            env=env,
            shell=True,
            stdout=output,
            stderr=subprocess.STDOUT,
            check=True,
        )


def classFactory(iface):  # pylint: disable=invalid-name
    context = setup_user_site()

    try:
        from .libre_geo_lens import LibreGeoLens
    except ImportError:
        install_dependencies(*context)
        from .libre_geo_lens import LibreGeoLens

    return LibreGeoLens(iface)
