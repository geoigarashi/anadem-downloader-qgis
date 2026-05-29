# -*- coding: utf-8 -*-
import os
import sys

from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsApplication
import processing

from .ANADEM_Downloader_provider import ANADEMDownloaderProvider

cmd_folder = os.path.dirname(__file__)

if cmd_folder not in sys.path:
    sys.path.insert(0, cmd_folder)


class ANADEMDownloaderPlugin(object):

    def __init__(self, iface):
        self.provider = None
        self.iface = iface

    def initProcessing(self):
        self.provider = ANADEMDownloaderProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()

        icon_path = os.path.join(cmd_folder, 'icon.png')
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        self.action = QAction(icon, 'ANADEM Downloader', self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu('Ferramentas Geo', self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        QgsApplication.processingRegistry().removeProvider(self.provider)
        self.iface.removePluginMenu('Ferramentas Geo', self.action)
        self.iface.removeToolBarIcon(self.action)

    def run(self):
        processing.execAlgorithmDialog('ANADEM Downloader:ANADEM Downloader')
