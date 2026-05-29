# -*- coding: utf-8 -*-
import os
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsProcessingProvider
from .ANADEM_Downloader_algorithm import ANADEMDownloaderAlgorithm


class ANADEMDownloaderProvider(QgsProcessingProvider):

    def __init__(self):
        QgsProcessingProvider.__init__(self)

    def unload(self):
        pass

    def loadAlgorithms(self):
        self.addAlgorithm(ANADEMDownloaderAlgorithm())

    def id(self):
        return 'ANADEM Downloader'

    def name(self):
        return self.tr('ANADEM Downloader')

    def icon(self):
        cmd_folder = os.path.dirname(__file__)
        icon_path = os.path.join(cmd_folder, 'icon.png')
        if os.path.exists(icon_path):
            return QIcon(icon_path)
        return QgsProcessingProvider.icon(self)

    def longName(self):
        return self.name()
