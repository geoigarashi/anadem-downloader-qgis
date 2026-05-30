# -*- coding: utf-8 -*-
import os
import sys

from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsApplication
import processing

from .Fluxo_Topografico_provider import FluxoTopograficoProvider

cmd_folder = os.path.dirname(__file__)

if cmd_folder not in sys.path:
    sys.path.insert(0, cmd_folder)


class FluxoTopograficoPlugin(object):

    def __init__(self, iface) -> None:
        self.provider: FluxoTopograficoProvider | None = None
        self.iface = iface

    def initProcessing(self) -> None:
        self.provider = FluxoTopograficoProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self) -> None:
        self.initProcessing()

        icon_path = os.path.join(cmd_folder, 'icon.png')
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        self.action = QAction(icon, 'Fluxo Topográfico', self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu('Ferramentas Geo', self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self) -> None:
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider)
        self.iface.removePluginMenu('Ferramentas Geo', self.action)
        self.iface.removeToolBarIcon(self.action)

    def run(self) -> None:
        processing.execAlgorithmDialog('Fluxo Topográfico:fluxotopografico')
