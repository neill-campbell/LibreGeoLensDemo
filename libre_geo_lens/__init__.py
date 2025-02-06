def install_pip_and_dependencies():
    import os
    import subprocess
    from qgis.PyQt.QtCore import QStandardPaths
    python = QStandardPaths.findExecutable("python")
    if not python:
        import sys
        python = sys.executable
        if "MacOS" in python:
            python = python.replace("MacOS/QGIS", "MacOS/bin/python3")
    try:
        # Check if pip is installed
        subprocess.run([python, "-m", "pip", "--version"], check=True)
        print("pip is already installed.")
    except subprocess.CalledProcessError:
        print("pip is not installed. Installing pip...")
        try:
            subprocess.run([python, "-m", "ensurepip", "--default-pip"], check=True)
            print("pip has been installed successfully.")
        except subprocess.CalledProcessError:
            print("ensurepip failed. Trying get-pip.py...")
            try:
                import urllib.request
                url = "https://bootstrap.pypa.io/get-pip.py"
                get_pip_path = "get-pip.py"
                urllib.request.urlretrieve(url, get_pip_path)
                subprocess.run([python, get_pip_path], check=True)
                os.remove(get_pip_path)
                print("pip installed successfully using get-pip.py")
            except Exception as e:
                print(f"Failed to install pip: {e}")
    subprocess.check_call([python, "-m", "pip", "install", "-r",
                           os.path.join(os.path.dirname(__file__), "requirements.txt")])


def classFactory(iface):  # pylint: disable=invalid-name
    try:
        from .libre_geo_lens import LibreGeoLens
    except ImportError:
        try:
            install_pip_and_dependencies()
        except:
            raise Exception("Python dependencies failed to install. Please install them manually by following"
                            " https://github.com/ampsight/LibreGeoLens?tab=readme-ov-file#python-dependencies")
        try:
            from .libre_geo_lens import LibreGeoLens
        except ImportError:
            raise Exception("Please restart QGIS. If this error persists, please install the python dependencies"
                            " manually by following https://github.com/ampsight/LibreGeoLens?tab=readme-ov-file#python-dependencies")
    return LibreGeoLens(iface)
