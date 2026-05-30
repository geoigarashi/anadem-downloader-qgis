# -*- coding: utf-8 -*-
"""
/***************************************************************************
 ANADEM_Downloader
                                 A QGIS plugin
 Baixa o MDE ANADEM v1 para uma área de interesse
                              -------------------
        begin                : 2025-05-29
        copyright            : (C) 2025
        email                :
 ***************************************************************************/

 This script initializes the plugin, making it known to QGIS.
"""


def classFactory(iface):
    from .Fluxo_Topografico import FluxoTopograficoPlugin
    return FluxoTopograficoPlugin(iface)
