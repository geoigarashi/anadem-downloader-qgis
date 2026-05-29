# -*- coding: utf-8 -*-
"""
ANADEM Downloader — algoritmo principal.

Fluxo:
  1. Identifica tiles MGRS que intersectam a área de interesse (sem internet).
  2. Baixa apenas os tiles necessários (cache em disco).
  3. Recorta e mescla os tiles.
  4. Gera as saídas escolhidas: MDE, Curvas de Nível ou ambas.
"""

import os
import re
import shutil
import tempfile
from urllib.parse import urlparse

from osgeo import gdal, ogr, osr

from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QColor, QIcon, QPainter
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsAuthMethodConfig,
    QgsColorRampShader,
    QgsCoordinateReferenceSystem,
    QgsDistanceArea,
    QgsGeometry,
    QgsPalLayerSettings,
    QgsProcessingAlgorithm,
    QgsProcessingParameterAuthConfig,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterColor,
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
    QgsProcessingParameterNumber,
    QgsProject,
    QgsRasterBandStats,
    QgsRasterLayer,
    QgsRasterShader,
    QgsRuleBasedRenderer,
    QgsSingleBandPseudoColorRenderer,
    QgsSymbol,
    QgsSymbolLayerReference,
    QgsSymbolLayerId,
    QgsTextFormat,
    QgsTextMaskSettings,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)

from .gdal_calc import Calc


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
SAIDA_MDE = 0
SAIDA_CURVAS = 1
SAIDA_AMBOS = 2
AREA_MAXIMA_KM2 = 1000


class ANADEMDownloaderAlgorithm(QgsProcessingAlgorithm):

    AREA_INTERESSE = 'AREA_INTERESSE'
    SAIDA = 'SAIDA'
    HILLSHADE = 'HILLSHADE'
    INTERVALO = 'INTERVALO'
    SUAVIZACAO = 'SUAVIZACAO'
    COR_CURVAS = 'COR_CURVAS'
    MUDAR_CRS = 'MUDAR_CRS'
    AUTENTIC = 'AUTENTIC'

    def __init__(self):
        super().__init__()
        self.temp_dir = os.path.join(tempfile.gettempdir(), 'ANADEM_Downloader')
        self.status_total = 0.0
        self.progresso = 0.0
        self._plugin_dir = os.path.dirname(__file__)

    # ------------------------------------------------------------------
    # Flags e metadados
    # ------------------------------------------------------------------

    def flags(self):
        if Qgis.QGIS_VERSION_INT >= 40000:
            return super().flags() | Qgis.ProcessingAlgorithmFlag.NoThreading
        return super().flags() | QgsProcessingAlgorithm.FlagNoThreading

    def initAlgorithm(self, config):
        self.addParameter(
            QgsProcessingParameterExtent(
                self.AREA_INTERESSE,
                self.tr('Área de Interesse'),
                optional=False,
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                name=self.SAIDA,
                description=self.tr('Saída desejada'),
                options=[
                    self.tr('MDE — Modelo Digital de Elevação'),
                    self.tr('Curvas de Nível'),
                    self.tr('MDE + Curvas de Nível'),
                ],
                defaultValue=SAIDA_AMBOS,
                optional=False,
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                name=self.HILLSHADE,
                description=self.tr('Adicionar overlay de Hillshade'),
                defaultValue=True,
                optional=False,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                name=self.INTERVALO,
                description=self.tr('Intervalo entre curvas de nível (m)'),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=10,
                minValue=1,
                maxValue=1000,
                optional=False,
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                name=self.SUAVIZACAO,
                description=self.tr('Nível de suavização das curvas'),
                options=[
                    self.tr('Nenhum'),
                    self.tr('Baixo'),
                    self.tr('Médio'),
                    self.tr('Alto'),
                ],
                defaultValue=2,  # Médio
                optional=False,
            )
        )

        self.addParameter(
            QgsProcessingParameterColor(
                name=self.COR_CURVAS,
                description=self.tr('Cor das curvas de nível'),
                defaultValue='#cc7700cc',
                opacityEnabled=True,
                optional=False,
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                name=self.MUDAR_CRS,
                description=self.tr('Reprojetar o projeto para o CRS UTM detectado'),
                defaultValue=False,
                optional=False,
            )
        )

        self.addParameter(
            QgsProcessingParameterAuthConfig(
                name=self.AUTENTIC,
                description=self.tr('Autenticação de Proxy (opcional)'),
                optional=True,
            )
        )

    # ------------------------------------------------------------------
    # Algoritmo principal
    # ------------------------------------------------------------------

    def processAlgorithm(self, parameters, context, feedback):
        self.status_total = 0.0
        self.progresso = 0.0
        os.makedirs(self.temp_dir, exist_ok=True)
        feedback.pushInfo(f'\nPasta temporária: {self.temp_dir}')

        # ---- Parâmetros ----
        area_interesse = self.parameterAsExtent(
            parameters, self.AREA_INTERESSE, context,
            crs=QgsCoordinateReferenceSystem('EPSG:4326'),
        )
        if area_interesse.isNull() or not area_interesse.isFinite():
            raise ValueError(self.tr(
                'Área de interesse inválida. Desenhe um retângulo diretamente '
                'ou use uma camada salva.'))

        saida = self.parameterAsEnum(parameters, self.SAIDA, context)
        hillshade = self.parameterAsBool(parameters, self.HILLSHADE, context)
        intervalo = self.parameterAsInt(parameters, self.INTERVALO, context)
        suavizacao_idx = self.parameterAsEnum(parameters, self.SUAVIZACAO, context)
        suavizacao = ['Nenhum', 'Baixo', 'Médio', 'Alto'][suavizacao_idx]
        cor_curva = self.parameterAsColor(parameters, self.COR_CURVAS, context)
        mudar_crs = self.parameterAsBool(parameters, self.MUDAR_CRS, context)

        gerar_mde = saida in (SAIDA_MDE, SAIDA_AMBOS)
        gerar_curvas = saida in (SAIDA_CURVAS, SAIDA_AMBOS)

        # ---- Verificar área máxima ----
        da = QgsDistanceArea()
        da.setSourceCrs(QgsCoordinateReferenceSystem('EPSG:4326'),
                        context.project().transformContext())
        da.setEllipsoid('WGS84')
        area_km2 = da.measureArea(QgsGeometry.fromRect(area_interesse)) / 1e6
        feedback.pushInfo(f'\nÁrea de interesse: {area_km2:,.1f} km²')
        if area_km2 > AREA_MAXIMA_KM2:
            raise ValueError(self.tr(
                f'Área de interesse muito grande ({area_km2:,.0f} km²). '
                f'O limite máximo é {AREA_MAXIMA_KM2:,} km². '
                'Reduza a seleção e tente novamente.'))

        # ---- Proxy GDAL (HTTP range requests via vsicurl) ----
        self._configurar_proxy(parameters, context, feedback)

        # ---- Shapefile de área de interesse (para gdal.Warp cutline) ----
        geom_aoi = QgsGeometry.fromRect(area_interesse)
        caminho_shp_aoi = self._criar_shp_aoi(geom_aoi, feedback)

        # ---- Identificar tiles MGRS (sem internet) ----
        tiles_necessarios = self._identificar_tiles(area_interesse, geom_aoi, feedback)
        if not tiles_necessarios:
            feedback.pushInfo(
                '\nNenhum tile ANADEM encontrado para a área selecionada.'
                '\nO ANADEM v1 cobre apenas o território brasileiro.'
                '\nVerifique se a área de interesse está dentro do Brasil.')
            return {}

        # ---- Carregar mapa de URLs (links.txt) ----
        tile_urls = self._carregar_urls()
        tiles_sem_url = [t for t in tiles_necessarios if t not in tile_urls]
        if tiles_sem_url:
            feedback.pushInfo(
                f'\nAVISO: tiles sem URL em links.txt: {tiles_sem_url}'
                '\nEles serão ignorados.')
        tiles_necessarios = [t for t in tiles_necessarios if t in tile_urls]
        if not tiles_necessarios:
            feedback.pushInfo('\nNenhum tile disponível para download.')
            return {}

        # ---- Progresso ----
        n_etapas = 4 + len(tiles_necessarios) + (2 if gerar_curvas else 0)
        self.status_total = 100.0 / n_etapas

        # ---- Clip direto via vsicurl (sem download do tile completo) ----
        clips = self._clipar_tiles_direto(tiles_necessarios, tile_urls, caminho_shp_aoi, feedback)
        if not clips:
            feedback.pushInfo('\nNenhum tile processado com sucesso.')
            return {}

        if feedback.isCanceled():
            return {}

        # ---- Mesclar tiles recortados ----
        merged_path = os.path.join(self.temp_dir, 'merged.tif')
        feedback.pushInfo('\nMesclando tiles...')
        gdal.Warp(
            merged_path,
            clips,
            dstNodata=-32768,
            format='GTiff',
            callback=self._callback_gdal(feedback),
        )
        self.progresso += 1
        feedback.setProgress(int(self.progresso * self.status_total))

        if feedback.isCanceled():
            return {}

        # ---- Reprojetar para UTM SIRGAS 2000 (sistema métrico) ----
        epsg_utm = self._detectar_epsg_utm(area_interesse)
        merged_utm = os.path.join(self.temp_dir, 'merged_utm.tif')
        self._reprojetar_para_utm(merged_path, merged_utm, epsg_utm, feedback)
        merged_path = merged_utm

        self.progresso += 1
        feedback.setProgress(int(self.progresso * self.status_total))

        if feedback.isCanceled():
            return {}

        # ---- Preparar camadas (todo GDAL antes de qualquer addMapLayer) ----
        hs_layer, dem_layer = None, None
        if gerar_mde:
            hs_layer, dem_layer = self._preparar_mde(merged_path, hillshade, feedback)

        curvas_layer = None
        if gerar_curvas:
            merged_curvas = os.path.join(self.temp_dir, 'merged_curvas.tif')
            shutil.copy2(merged_path, merged_curvas)

            self._suavizar_terreno(merged_curvas, suavizacao, feedback)
            if feedback.isCanceled():
                return {}

            curvas_layer = self._gerar_curvas(
                merged_curvas, caminho_shp_aoi, intervalo, cor_curva,
                epsg_utm, context, feedback)

        # ---- Reprojetar projeto (depois de todo GDAL, antes das camadas) ----
        if mudar_crs:
            novo_crs = QgsCoordinateReferenceSystem(f'EPSG:{epsg_utm}')
            QgsProject.instance().setCrs(novo_crs)
            feedback.pushInfo(
                f'\nCRS do projeto alterado para EPSG:{epsg_utm} '
                f'(SIRGAS 2000 UTM Sul — zona {epsg_utm - 31960}S)')

        # ---- Inserir camadas (ordem: Hillshade→MDE→Curvas = base→topo) ----
        if hs_layer and hs_layer.isValid():
            QgsProject.instance().addMapLayer(hs_layer)
            feedback.pushInfo('  → Overlay Hillshade adicionado')

        if dem_layer and dem_layer.isValid():
            QgsProject.instance().addMapLayer(dem_layer)
            feedback.pushInfo('  → MDE adicionado ao projeto')
            try:
                from qgis.utils import iface
                if iface is not None:
                    iface.layerTreeView().refreshLayerSymbology(dem_layer.id())
            except Exception:
                pass

        if curvas_layer and curvas_layer.isValid():
            QgsProject.instance().addMapLayer(curvas_layer)
            feedback.pushInfo('  → Curvas de Nível adicionadas')

        return {}

    # ------------------------------------------------------------------
    # Identificação de tiles (100% local, sem internet)
    # ------------------------------------------------------------------

    def _identificar_tiles(self, area_interesse, geom_aoi, feedback):
        mgrs_path = os.path.join(
            self._plugin_dir, 'assets', 'anadem_mgrs', 'mgrs.shp')
        if not os.path.exists(mgrs_path):
            raise FileNotFoundError(
                f'Shapefile MGRS não encontrado: {mgrs_path}')

        ds = ogr.Open(mgrs_path)
        lyr = ds.GetLayer(0)

        aoi_wkt = geom_aoi.asWkt()
        aoi_ogr = ogr.CreateGeometryFromWkt(aoi_wkt)

        tiles = []
        feedback.pushInfo('\nIdentificando tiles MGRS necessários (busca local)...')
        lyr.ResetReading()
        for feat in lyr:
            geom_tile = feat.GetGeometryRef()
            if geom_tile and geom_tile.Intersects(aoi_ogr):
                mgrs_code = feat.GetField('mgrs')
                tiles.append(mgrs_code)
                feedback.pushInfo(f'  Tile necessário: {mgrs_code}')
        ds = None
        return tiles

    # ------------------------------------------------------------------
    # Carregamento de URLs (links.txt)
    # ------------------------------------------------------------------

    def _carregar_urls(self):
        links_path = os.path.join(self._plugin_dir, 'assets', 'links.txt')
        tile_urls = {}
        with open(links_path, 'r', encoding='utf-8') as f:
            for line in f:
                url = line.strip()
                if not url:
                    continue
                # Padrão: .../anadem_v1_23K.tif  →  código = '23K'
                basename = url.rsplit('/', 1)[-1]          # anadem_v1_23K.tif
                code = basename.replace('anadem_v1_', '').replace('.tif', '')
                tile_urls[code] = url
        return tile_urls

    # ------------------------------------------------------------------
    # Clip direto via vsicurl (sem download do tile completo)
    # ------------------------------------------------------------------

    def _clipar_tiles_direto(self, tiles, tile_urls, shp_aoi, feedback):
        """Lê apenas a porção da ROI via HTTP range requests (requer tile COG)."""
        clips = []
        feedback.pushInfo('\nClipando tiles via vsicurl...')
        for tile in tiles:
            if feedback.isCanceled():
                return clips

            clip_path = os.path.join(self.temp_dir, f'anadem_v1_{tile}_clip.tif')
            feedback.pushInfo(f'\nTile: {tile}')

            if os.path.exists(clip_path):
                feedback.pushInfo('  → clip em cache local')
                clips.append(clip_path)
            else:
                url = tile_urls[tile]
                vsicurl_path = f'/vsicurl/{url}'
                feedback.pushInfo(f'  → clipping via vsicurl: {url}')
                try:
                    ds = gdal.Warp(
                        clip_path,
                        vsicurl_path,
                        cutlineDSName=shp_aoi,
                        cropToCutline=True,
                        dstNodata=-32768,
                        srcSRS='EPSG:4326',
                        dstSRS='EPSG:4326',
                        format='GTiff',
                        callback=self._callback_gdal(feedback),
                    )
                    ds = None
                    if os.path.exists(clip_path):
                        size_mb = os.path.getsize(clip_path) / 1_048_576
                        feedback.pushInfo(f'  → salvo ({size_mb:.1f} MB da ROI)')
                        clips.append(clip_path)
                    else:
                        feedback.pushInfo('  → ERRO: clip não gerado')
                except Exception as e:
                    feedback.pushInfo(f'  → ERRO: {e}')

            self.progresso += 1
            feedback.setProgress(int(self.progresso * self.status_total))

        return clips

    # ------------------------------------------------------------------
    # Saída: MDE
    # ------------------------------------------------------------------

    def _preparar_mde(self, merged_path, hillshade, feedback):
        """Configura camadas MDE e Hillshade sem adicioná-las ao projeto.

        Retorna (hs_layer_ou_None, dem_layer_ou_None).
        Separado do addMapLayer para evitar race condition com o thread de
        renderização do QGIS durante operações GDAL subsequentes.
        """
        feedback.pushInfo('\nPreparando camada MDE...')

        hs_layer = None
        if hillshade:
            hs_path = os.path.join(self.temp_dir, 'hillshade_overlay.tif')
            shutil.copy2(merged_path, hs_path)

            qml_path = os.path.splitext(hs_path)[0] + '.qml'
            with open(qml_path, 'w', encoding='utf-8') as f:
                f.write(self._qml_hillshade())

            hs_layer = QgsRasterLayer(hs_path, 'ANADEM v1 — Hillshade')
            if not hs_layer.isValid():
                feedback.pushInfo('  AVISO: Hillshade não pôde ser carregado.')
                hs_layer = None

        dem_layer = QgsRasterLayer(merged_path, 'ANADEM v1 — MDE')
        if not dem_layer.isValid():
            feedback.pushInfo('AVISO: Não foi possível carregar o MDE.')
            return hs_layer, None

        self._aplicar_rampa_elevacao(dem_layer, feedback)
        dem_layer.setBlendMode(QPainter.CompositionMode_Multiply)

        return hs_layer, dem_layer

    def _aplicar_rampa_elevacao(self, layer, feedback):
        """Rampa hipsométrica adaptada ao intervalo de elevação do raster."""
        try:
            stats = layer.dataProvider().bandStatistics(
                1, QgsRasterBandStats.Min | QgsRasterBandStats.Max)
            vmin = max(stats.minimumValue, 0.0)
            vmax = max(stats.maximumValue, vmin + 1.0)
        except Exception:
            vmin, vmax = 0.0, 3000.0

        feedback.pushInfo(f'  Intervalo de elevação: {vmin:.0f}m – {vmax:.0f}m')

        def interp(lo, hi, t):
            return lo + (hi - lo) * t

        # Cores em formato RGBA para cada nível relativo
        stops = [
            (0.00, QColor('#0a6e0a')),   # nível do mar / baixadas
            (0.10, QColor('#4caf50')),   # planícies
            (0.25, QColor('#c8b400')),   # cerrado / planalto baixo
            (0.50, QColor('#d4862a')),   # planalto médio
            (0.75, QColor('#9b5e2a')),   # serras
            (1.00, QColor('#e8e8e8')),   # pontos mais altos
        ]

        items = []
        for t, color in stops:
            val = vmin + (vmax - vmin) * t
            items.append(QgsColorRampShader.ColorRampItem(val, color, f'{val:.0f} m'))

        shader_func = QgsColorRampShader()
        shader_func.setColorRampType(QgsColorRampShader.Interpolated)
        shader_func.setColorRampItemList(items)

        raster_shader = QgsRasterShader()
        raster_shader.setRasterShaderFunction(shader_func)

        renderer = QgsSingleBandPseudoColorRenderer(
            layer.dataProvider(), 1, raster_shader)
        renderer.setClassificationMin(vmin)
        renderer.setClassificationMax(vmax)
        layer.setRenderer(renderer)
        layer.triggerRepaint()

    @staticmethod
    def _qml_hillshade():
        return (
            "<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n"
            "<qgis version='3.0' styleCategories='AllStyleCategories'>\n"
            "  <pipe>\n"
            "    <provider>\n"
            "      <resampling enabled='true' maxOversampling='2'"
            " zoomedInResamplingMethod='cubic'"
            " zoomedOutResamplingMethod='cubic'/>\n"
            "    </provider>\n"
            "    <rasterrenderer type='hillshade' band='1' opacity='1'"
            " alphaBand='-1' azimuth='315' angle='45'"
            " multidirectionlighting='0' zFactor='1'>\n"
            "      <rasterTransparency/>\n"
            "    </rasterrenderer>\n"
            "    <brightnesscontrast brightness='0' contrast='0' gamma='1'/>\n"
            "    <huesaturation saturation='0' grayscaleMode='0'"
            " colorizeOn='0' colorizeRed='255' colorizeGreen='128'"
            " colorizeBlue='128' colorizeStrength='100' invertColors='0'/>\n"
            "    <rasterresampler maxOversampling='2'"
            " zoomedInResampler='cubic' zoomedOutResampler='cubic'/>\n"
            "  </pipe>\n"
            "  <blendMode>0</blendMode>\n"
            "</qgis>\n"
        )

    # ------------------------------------------------------------------
    # Saída: Curvas de Nível
    # ------------------------------------------------------------------

    def _gerar_curvas(self, dem_path, shp_aoi, intervalo, cor_curva,
                      epsg_utm, context, feedback):
        feedback.pushInfo('\nGerando curvas de nível...')

        shp_driver = ogr.GetDriverByName('ESRI Shapefile')
        tmp_shp_dir = tempfile.mkdtemp(dir=self.temp_dir, prefix='curvas_')
        caminho_shp = os.path.join(tmp_shp_dir, 'curvas.shp')

        shp_ds = shp_driver.CreateDataSource(caminho_shp)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(epsg_utm)
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        lyr = shp_ds.CreateLayer('Curvas De Nivel', srs=srs)
        lyr.CreateField(ogr.FieldDefn('ID', ogr.OFTInteger))
        lyr.CreateField(ogr.FieldDefn('ELEV', ogr.OFTReal))
        fd = ogr.FieldDefn('TYPE', ogr.OFTString)
        fd.SetWidth(50)
        lyr.CreateField(fd)

        raster_ds = gdal.Open(dem_path)
        band = raster_ds.GetRasterBand(1)
        nodata = band.GetNoDataValue()
        gdal.ContourGenerate(
            band, intervalo, 0, [],
            1 if nodata is not None else 0,
            nodata if nodata is not None else 0,
            lyr, 0, 1,
            callback=self._callback_gdal(feedback),
        )
        shp_ds = None
        raster_ds = None

        self.progresso += 1
        feedback.setProgress(int(self.progresso * self.status_total))

        if feedback.isCanceled():
            return None

        # Reprojeção se o CRS do projeto diferir do UTM gerado
        project_crs = context.project().crs()
        utm_authid = f'EPSG:{epsg_utm}'
        if project_crs.isValid() and project_crs.authid().upper() != utm_authid.upper():
            feedback.pushInfo(
                f'\nReprojectando curvas para {project_crs.authid()}...')
            tmp_reproj = tempfile.mkdtemp(dir=self.temp_dir, prefix='curvas_reproj_')
            shp_reproj = os.path.join(tmp_reproj, 'curvas_reproj.shp')
            gdal.VectorTranslate(
                shp_reproj, caminho_shp,
                options=gdal.VectorTranslateOptions(
                    srcSRS=utm_authid,
                    dstSRS=project_crs.authid(),
                    reproject=True))
            caminho_shp_final = shp_reproj
        else:
            caminho_shp_final = caminho_shp

        layer = QgsVectorLayer(caminho_shp_final, 'ANADEM v1 — Curvas de Nível')
        n_curvas = len(list(layer.getFeatures()))
        feedback.pushInfo(f'  {n_curvas} curvas geradas')

        self._estilizar_curvas(layer, intervalo, cor_curva)

        self.progresso += 1
        feedback.setProgress(int(self.progresso * self.status_total))

        return layer

    def _estilizar_curvas(self, layer, intervalo, cor_curva):
        """Simbologia com curva mestra/normal, rótulos e máscara (igual ao Curva de Nível)."""
        symbol = QgsSymbol.defaultSymbol(layer.geometryType())
        renderer = QgsRuleBasedRenderer(symbol)
        root_rule = renderer.rootRule()

        # Curva Mestra (a cada 5× o intervalo)
        regra_mestra = root_rule.children()[0]
        regra_mestra.setLabel('Curva Mestra')
        regra_mestra.setFilterExpression(f'"ELEV" % {intervalo * 5} = 0')
        regra_mestra.symbol().setColor(cor_curva)
        regra_mestra.symbol().setWidth(0.5)

        # Curva Normal
        regra_normal = root_rule.children()[0].clone()
        regra_normal.setLabel('Curva Normal')
        regra_normal.setFilterExpression('ELSE')
        regra_normal.symbol().setColor(cor_curva)
        regra_normal.symbol().setWidth(0.25)
        root_rule.appendChild(regra_normal)

        layer.setRenderer(renderer)
        layer.triggerRepaint()

        # Rótulos com máscara nas curvas mestras
        mask = QgsTextMaskSettings()
        mask.setSize(2)
        if Qgis.QGIS_VERSION_INT < 33000:
            mask.setMaskedSymbolLayers([QgsSymbolLayerReference(
                layer.id(), QgsSymbolLayerId(regra_mestra.ruleKey(), 0))])
        else:
            mask.setMaskedSymbolLayers([QgsSymbolLayerReference(
                layer.id(), regra_mestra.symbol().symbolLayer(0).id())])
        mask.setEnabled(True)

        fmt = QgsTextFormat()
        fmt.setSize(10)
        fmt.setColor(cor_curva)
        fmt.setMask(mask)

        pal = QgsPalLayerSettings()
        pal.fieldName = (
            f'CASE WHEN "ELEV" % {intervalo * 5} = 0 '
            f'THEN "ELEV" ELSE \'\' END')
        pal.enabled = True
        pal.drawLabels = True
        pal.repeatDistance = 50
        pal.isExpression = True
        if Qgis.QGIS_VERSION_INT >= 40000:
            pal.placement = Qgis.LabelPlacement.Line
            pal.placementFlags = Qgis.LabelLinePlacementFlag.OnLine
        else:
            pal.placement = QgsPalLayerSettings.Line
            pal.placementFlags = QgsPalLayerSettings.OnLine
        pal.setFormat(fmt)

        layer.setLabelsEnabled(True)
        layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
        layer.triggerRepaint()

    # ------------------------------------------------------------------
    # Suavização TPI-ponderada (idêntica ao Curva de Nível)
    # ------------------------------------------------------------------

    def _suavizar_terreno(self, dem_path, suavizacao, feedback):
        if suavizacao == 'Nenhum':
            return

        feedback.pushInfo(f'\nSuavizando terreno ({suavizacao})...')
        path = self.temp_dir

        gdal.Translate(
            os.path.join(path, 'dem_s.tif'), dem_path,
            options='-ot Float32 -a_nodata -32768')

        def _make_vrt(src, dst, size, coefs):
            gdal.BuildVRT(dst, src)
            with open(dst, 'rt') as f:
                data = f.read()
            data = data.replace('ComplexSource', 'KernelFilteredSource')
            nodata_tag = '<NODATA>-32768</NODATA>'
            kernel = (
                f'<Kernel normalized="1">'
                f'<Size>{size}</Size>'
                f'<Coefs>{coefs}</Coefs>'
                f'</Kernel>'
            )
            data = data.replace(nodata_tag, nodata_tag + kernel)
            with open(dst, 'wt') as f:
                f.write(data)

        coefs_3x3 = (
            '0.077847 0.123317 0.077847 '
            '0.123317 0.195346 0.123317 '
            '0.077847 0.123317 0.077847')
        _make_vrt(
            os.path.join(path, 'dem_s.tif'),
            os.path.join(path, 'dem_blur_3x3.vrt'),
            3, coefs_3x3)

        gdal.DEMProcessing(
            os.path.join(path, 'dem_tpi.tif'),
            os.path.join(path, 'dem_s.tif'),
            'TPI')

        Calc(
            calc='((-1)*A*(A<0))+(A*(A>=0))',
            A=os.path.join(path, 'dem_tpi.tif'),
            outfile=os.path.join(path, 'tpi_pos.tif'),
            NoDataValue=-32768, overwrite=True)

        coefs_9x9 = (
            '0 0.000001 0.000014 0.000055 0.000088 0.000055 0.000014 0.000001 0 '
            '0.000001 0.000036 0.000362 0.001445 0.002289 0.001445 0.000362 0.000036 0.000001 '
            '0.000014 0.000362 0.003672 0.014648 0.023205 0.014648 0.003672 0.000362 0.000014 '
            '0.000055 0.001445 0.014648 0.058434 0.092566 0.058434 0.014648 0.001445 0.000055 '
            '0.000088 0.002289 0.023205 0.092566 0.146634 0.092566 0.023205 0.002289 0.000088 '
            '0.000055 0.001445 0.014648 0.058434 0.092566 0.058434 0.014648 0.001445 0.000055 '
            '0.000014 0.000362 0.003672 0.014648 0.023205 0.014648 0.003672 0.000362 0.000014 '
            '0.000001 0.000036 0.000362 0.001445 0.002289 0.001445 0.000362 0.000036 0.000001 '
            '0 0.000001 0.000014 0.000055 0.000088 0.000055 0.000014 0.000001 0')
        _make_vrt(
            os.path.join(path, 'tpi_pos.tif'),
            os.path.join(path, 'tpi_blur_9x9.vrt'),
            9, coefs_9x9)

        try:
            info = gdal.Info(os.path.join(path, 'tpi_blur_9x9.vrt'),
                             options='-hist -stats')
            max_val = re.findall(
                r'[0-9]*\.[0-9]*',
                re.findall(r'STATISTICS_MAXIMUM=\d*\.\d*', info)[0])[0]
            Calc(
                calc=f'A / {max_val}',
                A=os.path.join(path, 'tpi_blur_9x9.vrt'),
                outfile=os.path.join(path, 'tpi_norm.tif'),
                NoDataValue=-32768, overwrite=True)
        except Exception:
            gdal.Translate(
                os.path.join(path, 'tpi_norm.tif'),
                os.path.join(path, 'tpi_blur_9x9.vrt'))

        if suavizacao == 'Baixo':
            Calc(
                calc='A*B+(1-A)*C',
                A=os.path.join(path, 'tpi_norm.tif'),
                B=os.path.join(path, 'dem_blur_3x3.vrt'),
                C=os.path.join(path, 'dem_blur_3x3.vrt'),
                outfile=dem_path, overwrite=True)

        elif suavizacao == 'Médio':
            coefs_7x7 = (
                '0.000036 0.000363 0.001446 0.002291 0.001446 0.000363 0.000036 '
                '0.000363 0.003676 0.014662 0.023226 0.014662 0.003676 0.000363 '
                '0.001446 0.014662 0.058488 0.092651 0.058488 0.014662 0.001446 '
                '0.002291 0.023226 0.092651 0.146768 0.092651 0.023226 0.002291 '
                '0.001446 0.014662 0.058488 0.092651 0.058488 0.014662 0.001446 '
                '0.000363 0.003676 0.014662 0.023226 0.014662 0.003676 0.000363 '
                '0.000036 0.000363 0.001446 0.002291 0.001446 0.000363 0.000036')
            _make_vrt(
                os.path.join(path, 'dem_s.tif'),
                os.path.join(path, 'dem_blur_7x7.vrt'),
                7, coefs_7x7)
            Calc(
                calc='A*B+(1-A)*C',
                A=os.path.join(path, 'tpi_norm.tif'),
                B=os.path.join(path, 'dem_blur_3x3.vrt'),
                C=os.path.join(path, 'dem_blur_7x7.vrt'),
                outfile=dem_path, overwrite=True)

        else:  # Alto
            # Kernel Gaussiano 13×13, sigma=2.5 — exatamente 169 coeficientes, normalizado
            coefs_13x13 = (
                '0.000082 0.000197 0.000405 0.000708 0.001057 0.001343 0.001455 0.001343 0.001057 0.000708 0.000405 0.000197 0.000082 '
                '0.000197 0.000475 0.000975 0.001708 0.002547 0.003238 0.003508 0.003238 0.002547 0.001708 0.000975 0.000475 0.000197 '
                '0.000405 0.000975 0.002004 0.003508 0.005234 0.006653 0.007207 0.006653 0.005234 0.003508 0.002004 0.000975 0.000405 '
                '0.000708 0.001708 0.003508 0.006142 0.009162 0.011648 0.012618 0.011648 0.009162 0.006142 0.003508 0.001708 0.000708 '
                '0.001057 0.002547 0.005234 0.009162 0.013669 0.017376 0.018823 0.017376 0.013669 0.009162 0.005234 0.002547 0.001057 '
                '0.001343 0.003238 0.006653 0.011648 0.017376 0.022089 0.023929 0.022089 0.017376 0.011648 0.006653 0.003238 0.001343 '
                '0.001455 0.003508 0.007207 0.012618 0.018823 0.023929 0.025922 0.023929 0.018823 0.012618 0.007207 0.003508 0.001455 '
                '0.001343 0.003238 0.006653 0.011648 0.017376 0.022089 0.023929 0.022089 0.017376 0.011648 0.006653 0.003238 0.001343 '
                '0.001057 0.002547 0.005234 0.009162 0.013669 0.017376 0.018823 0.017376 0.013669 0.009162 0.005234 0.002547 0.001057 '
                '0.000708 0.001708 0.003508 0.006142 0.009162 0.011648 0.012618 0.011648 0.009162 0.006142 0.003508 0.001708 0.000708 '
                '0.000405 0.000975 0.002004 0.003508 0.005234 0.006653 0.007207 0.006653 0.005234 0.003508 0.002004 0.000975 0.000405 '
                '0.000197 0.000475 0.000975 0.001708 0.002547 0.003238 0.003508 0.003238 0.002547 0.001708 0.000975 0.000475 0.000197 '
                '0.000082 0.000197 0.000405 0.000708 0.001057 0.001343 0.001455 0.001343 0.001057 0.000708 0.000405 0.000197 0.000082')
            _make_vrt(
                os.path.join(path, 'dem_s.tif'),
                os.path.join(path, 'dem_blur_13x13.vrt'),
                13, coefs_13x13)
            Calc(
                calc='A*B+(1-A)*C',
                A=os.path.join(path, 'tpi_norm.tif'),
                B=os.path.join(path, 'dem_blur_3x3.vrt'),
                C=os.path.join(path, 'dem_blur_13x13.vrt'),
                outfile=dem_path, overwrite=True)

    # ------------------------------------------------------------------
    # Utilitários
    # ------------------------------------------------------------------

    def _detectar_epsg_utm(self, area_interesse):
        """Retorna o EPSG SIRGAS 2000 UTM Sul adequado ao centroide da AOI."""
        lon_centro = (area_interesse.xMinimum() + area_interesse.xMaximum()) / 2
        zona = int((lon_centro + 180) / 6) + 1
        epsg = 31960 + zona   # zona 18→31978, zona 22→31982, zona 23→31983
        return epsg

    def _reprojetar_para_utm(self, src_path, dst_path, epsg, feedback):
        """Reprojeta o raster mesclado para SIRGAS 2000 UTM Sul (sistema métrico)."""
        feedback.pushInfo(f'\nReprojetando MDE para EPSG:{epsg} (SIRGAS 2000 UTM Sul)...')
        gdal.Warp(
            dst_path,
            src_path,
            dstSRS=f'EPSG:{epsg}',
            resampleAlg=gdal.GRA_Bilinear,
            dstNodata=-32768,
            format='GTiff',
            callback=self._callback_gdal(feedback),
        )

    def _criar_shp_aoi(self, geom_aoi, feedback):
        shp_driver = ogr.GetDriverByName('ESRI Shapefile')
        path = os.path.join(self.temp_dir, 'aoi.shp')
        if os.path.exists(path):
            shp_driver.DeleteDataSource(path)
        ds = shp_driver.CreateDataSource(path)
        lyr = ds.CreateLayer('aoi', geom_type=ogr.wkbPolygon)
        feat = ogr.Feature(lyr.GetLayerDefn())
        feat.SetGeometry(ogr.CreateGeometryFromWkt(geom_aoi.asWkt()))
        lyr.CreateFeature(feat)
        ds = None
        return path

    def _configurar_proxy(self, parameters, context, feedback):
        autentic = self.parameterAsString(parameters, self.AUTENTIC, context)
        if not autentic:
            feedback.pushInfo('\nSem autenticação de proxy')
            return
        try:
            auth_mgr = QgsApplication.authManager()
            cfg = QgsAuthMethodConfig()
            auth_mgr.loadAuthenticationConfig(autentic, cfg, True)
            info = cfg.configMap()
            host = urlparse(info['realm']).hostname
            port = urlparse(info['realm']).port
            user = info['username']
            pwd = info['password']
            gdal.SetConfigOption('GDAL_HTTP_PROXY', f'{host}:{port}')
            gdal.SetConfigOption('GDAL_HTTP_PROXYUSERPWD', f'{user}:{pwd}')
            feedback.pushInfo(f'\nProxy GDAL configurado: {user}@{host}:{port}')
        except Exception as e:
            feedback.pushInfo(f'\nErro ao configurar proxy: {e}')

    def _callback_gdal(self, feedback):
        def cb(progress, msg, data):
            pct = self.progresso + progress
            feedback.setProgress(int(pct * self.status_total))
        return cb

    # ------------------------------------------------------------------
    # Metadados
    # ------------------------------------------------------------------

    def icon(self):
        icon_path = os.path.join(self._plugin_dir, 'icon.png')
        if os.path.exists(icon_path):
            return QIcon(icon_path)
        return super().icon()

    def name(self):
        return 'ANADEM Downloader'

    def displayName(self):
        return self.tr(self.name())

    def group(self):
        return self.tr(self.groupId())

    def groupId(self):
        return ''

    def shortHelpString(self):
        icon_path = os.path.join(os.path.dirname(__file__), 'icon.png').replace('\\', '/')
        return (
            f'<p align="center"><img src="file:///{icon_path}" width="72" height="72"/></p>'
            + self.tr(
                'Baixa tiles do MDE ANADEM v1 (30m, Brasil) para uma área de '
                'interesse e gera as saídas selecionadas:\n\n'
                '• MDE: raster com rampa hipsométrica adaptada ao intervalo de '
                'elevação + overlay opcional de Hillshade (blendMode Multiply).\n\n'
                '• Curvas de Nível: vetores com suavização TPI-ponderada, '
                'curvas mestras e normais, rótulos com máscara.\n\n'
                '• MDE + Curvas de Nível: ambas as saídas.\n\n'
                'Os tiles são selecionados localmente pelo índice MGRS '
                '(sem consulta à internet) e armazenados em cache após o '
                'primeiro download, reduzindo o consumo de dados nas '
                'execuções seguintes.'
            )
        )

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return ANADEMDownloaderAlgorithm()
