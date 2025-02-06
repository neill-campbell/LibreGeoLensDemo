from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QIcon, QColor
from qgis.PyQt.QtWidgets import QAction, QFileDialog, QMessageBox

from .resources import *
from .dock import LibreGeoLensDockWidget
from .settings import SettingsDialog


class LibreGeoLens:
    def __init__(self, iface):
        self.iface = iface
        self.name = '&LibreGeoLens'
        self.actions = []
        self.dock_widget = None

    def initGui(self):
        self.add_action("Settings", ":/plugins/libre_geo_lens/resources/icons/settings_icon.png", self.open_settings)
        self.add_action("Run", ":/plugins/libre_geo_lens/resources/icons/icon.png", self.run)

    def add_action(self, name, icon_resource_str, fn_to_connect):
        action = QAction(QIcon(icon_resource_str), name, self.iface.mainWindow())
        action.triggered.connect(fn_to_connect)
        self.iface.addToolBarIcon(action)
        self.iface.addPluginToMenu(self.name, action)
        self.actions.append(action)

    def open_settings(self):
        settings_dialog = SettingsDialog(self.iface.mainWindow())
        settings_dialog.exec_()

    def run(self):
        if self.dock_widget is None:
            self.dock_widget = LibreGeoLensDockWidget(self.iface)
        self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock_widget)
        self.dock_widget.setAllowedAreas(Qt.RightDockWidgetArea)
        self.dock_widget.show()

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.name, action)
            self.iface.removeToolBarIcon(action)
        if self.dock_widget:
            self.iface.removeDockWidget(self.dock_widget)
