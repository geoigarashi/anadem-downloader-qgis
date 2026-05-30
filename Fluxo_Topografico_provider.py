# -*- coding: utf-8 -*-
import os
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsProcessingProvider
from .Fluxo_Topografico_algorithm import FluxoTopograficoAlgorithm


class FluxoTopograficoProvider(QgsProcessingProvider):

    def __init__(self) -> None:
        QgsProcessingProvider.__init__(self)

    def unload(self) -> None:
        pass

    def loadAlgorithms(self) -> None:
        self.addAlgorithm(FluxoTopograficoAlgorithm())

    def id(self) -> str:
        return 'Fluxo Topográfico'

    def name(self) -> str:
        return self.tr('Fluxo Topográfico')

    def icon(self) -> QIcon:
        cmd_folder = os.path.dirname(__file__)
        icon_path = os.path.join(cmd_folder, 'icon.png')
        if os.path.exists(icon_path):
            return QIcon(icon_path)
        return QgsProcessingProvider.icon(self)

    def longName(self) -> str:
        return self.name()
