# -*- coding: utf-8 -*-
"""
Fluxo Topográfico — algoritmo principal.

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
    QgsProcessingContext,
    QgsProcessingFeedback,
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
AREA_MAXIMA_KM2 = 10000


class FluxoTopograficoAlgorithm(QgsProcessingAlgorithm):

    AREA_INTERESSE = 'AREA_INTERESSE'
    SAIDA = 'SAIDA'
    HILLSHADE = 'HILLSHADE'
    INTERVALO = 'INTERVALO'
    SUAVIZACAO = 'SUAVIZACAO'
    COR_CURVAS = 'COR_CURVAS'
    MUDAR_CRS = 'MUDAR_CRS'
    AUTENTIC = 'AUTENTIC'
    GERAR_DECLIVIDADE = 'GERAR_DECLIVIDADE'
    ESTILO_DECLIVIDADE = 'ESTILO_DECLIVIDADE'
    DECLIVIDADE_CATEGORICA = 'DECLIVIDADE_CATEGORICA'

    def __init__(self):
        super().__init__()
        self.temp_dir = os.path.join(tempfile.gettempdir(), 'Fluxo_Topografico')
        self.status_total = 0.0
        self.progresso = 0.0
        self._plugin_dir = os.path.dirname(__file__)

    # ------------------------------------------------------------------
    # Flags e metadados
    # ------------------------------------------------------------------

    def flags(self) -> int:
        if Qgis.QGIS_VERSION_INT >= 40000:
            return super().flags() | Qgis.ProcessingAlgorithmFlag.NoThreading
        return super().flags() | QgsProcessingAlgorithm.FlagNoThreading

    def initAlgorithm(self, config: dict | None = None) -> None:
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

        self.addParameter(
            QgsProcessingParameterBoolean(
                name=self.GERAR_DECLIVIDADE,
                description=self.tr('Criar raster de declividade'),
                defaultValue=False,
                optional=False,
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                name=self.ESTILO_DECLIVIDADE,
                description=self.tr('Simbologia e unidade da declividade'),
                options=[
                    self.tr('Sem estilo'),
                    self.tr('Declividade FAO (%)'),
                    self.tr('Declividade Embrapa (%)'),
                    self.tr('Declividade CAR (°)'),
                ],
                defaultValue=3,  # Padrão: CAR (°)
                optional=False,
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                name=self.DECLIVIDADE_CATEGORICA,
                description=self.tr('Classificar raster (declividade categórica)'),
                defaultValue=False,
                optional=False,
            )
        )

    # ------------------------------------------------------------------
    # Algoritmo principal
    # ------------------------------------------------------------------

    def processAlgorithm(self, parameters: dict, context: QgsProcessingContext, feedback: QgsProcessingFeedback) -> dict:
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
        gerar_declividade = self.parameterAsBool(parameters, self.GERAR_DECLIVIDADE, context)
        estilo_declividade = self.parameterAsEnum(parameters, self.ESTILO_DECLIVIDADE, context)
        declividade_categorica = self.parameterAsBool(parameters, self.DECLIVIDADE_CATEGORICA, context)

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
        n_etapas = 4 + len(tiles_necessarios) + (2 if gerar_curvas else 0) + (1 if gerar_declividade else 0)
        self.status_total = 100.0 / n_etapas

        # ---- Clip direto via vsicurl (sem download do tile completo) ----
        clips = self._clipar_tiles_direto(tiles_necessarios, tile_urls, caminho_shp_aoi, area_interesse, feedback)
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

        slope_layer = None
        if gerar_declividade:
            slope_layer = self._preparar_declividade(
                merged_path, estilo_declividade, declividade_categorica, feedback
            )
            self.progresso += 1
            feedback.setProgress(int(self.progresso * self.status_total))

        # ---- Reprojetar projeto (depois de todo GDAL, antes das camadas) ----
        if mudar_crs:
            novo_crs = QgsCoordinateReferenceSystem(f'EPSG:{epsg_utm}')
            QgsProject.instance().setCrs(novo_crs)
            feedback.pushInfo(
                f'\nCRS do projeto alterado para EPSG:{epsg_utm} '
                f'(SIRGAS 2000 UTM Sul — zona {epsg_utm - 31960}S)')

        # ---- Inserir camadas agrupadas (ordem final: Curvas→Declividade→MDE→Hillshade = topo→base) ----
        root = QgsProject.instance().layerTreeRoot()
        group_name = self.tr('Resultados do Fluxo Topográfico')
        group = root.findGroup(group_name)
        if not group:
            group = root.insertGroup(0, group_name)

        if hs_layer and hs_layer.isValid():
            QgsProject.instance().addMapLayer(hs_layer, False)
            group.insertLayer(0, hs_layer)
            feedback.pushInfo('  → Overlay Hillshade adicionado ao grupo')

        if dem_layer and dem_layer.isValid():
            QgsProject.instance().addMapLayer(dem_layer, False)
            group.insertLayer(0, dem_layer)
            feedback.pushInfo('  → MDE adicionado ao grupo')
            try:
                from qgis.utils import iface
                if iface is not None:
                    iface.layerTreeView().refreshLayerSymbology(dem_layer.id())
            except Exception:
                pass

        if slope_layer and slope_layer.isValid():
            QgsProject.instance().addMapLayer(slope_layer, False)
            group.insertLayer(0, slope_layer)
            feedback.pushInfo('  → Raster de Declividade adicionado ao grupo')

        if curvas_layer and curvas_layer.isValid():
            QgsProject.instance().addMapLayer(curvas_layer, False)
            group.insertLayer(0, curvas_layer)
            feedback.pushInfo('  → Curvas de Nível adicionadas ao grupo')

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

    def _clipar_tiles_direto(self, tiles, tile_urls, shp_aoi, area_interesse, feedback):
        """Lê apenas a porção da ROI via HTTP range requests (requer tile COG)."""
        clips = []
        feedback.pushInfo('\nClipando tiles via vsicurl...')

        # Assinatura espacial da AOI: garante que cache de áreas diferentes não colida
        aoi_hash = (
            f"{area_interesse.xMinimum():.4f}_{area_interesse.yMinimum():.4f}"
            f"_{area_interesse.xMaximum():.4f}_{area_interesse.yMaximum():.4f}"
        )
        aoi_id = re.sub(r'[^0-9]', '', aoi_hash)

        for tile in tiles:
            if feedback.isCanceled():
                return clips

            clip_path = os.path.join(self.temp_dir, f'anadem_v1_{tile}_{aoi_id}_clip.tif')
            feedback.pushInfo(f'\nTile: {tile}')

            if os.path.exists(clip_path):
                feedback.pushInfo('  → clip em cache local')
                clips.append(clip_path)
            else:
                url = tile_urls[tile]
                vsicurl_path = f'/vsicurl/{url}'
                feedback.pushInfo(f'  → clipping via vsicurl: {url}')
                try:
                    _ds = gdal.Warp(
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
                    _ds = None
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
        return 'fluxotopografico'

    def displayName(self):
        return self.tr('Fluxo Topográfico')

    def group(self):
        return self.tr(self.groupId())

    def groupId(self):
        return ''

    def shortHelpString(self) -> str:
        icon_path = os.path.join(os.path.dirname(__file__), 'icon.png').replace('\\', '/')
        logo_path = os.path.join(os.path.dirname(__file__), 'Logo-GEO-HQ.svg').replace('\\', '/')
        return (
            f'<p align="center">'
            f'<img src="file:///{icon_path}" height="100" style="vertical-align: middle; margin-right: 20px;"/>'
            f'<img src="file:///{logo_path}" height="100" style="vertical-align: middle;"/>'
            f'</p>'
            + self.tr(
                'Baixa tiles do MDE ANADEM v1 (30m, Brasil) para uma área de '
                'interesse e gera as saídas selecionadas:\n\n'
                '• MDE: raster com rampa hipsométrica adaptada ao intervalo de '
                'elevação + overlay opcional de Hillshade (blendMode Multiply).\n\n'
                '• Curvas de Nível: vetores com suavização TPI-ponderada, '
                'curvas mestras e normais, rótulos com máscara.\n\n'
                '• MDE + Curvas de Nível: ambas as saídas.\n\n'
                '• Declividade: raster de declividade temática gerado a partir do MDE. '
                'Estilos de simbologia disponíveis:\n'
                '  - FAO (%): classificação de declividade em porcentagem seguindo o padrão FAO.\n'
                '  - Embrapa (%): classificação de declividade em porcentagem conforme a Embrapa.\n'
                '  - CAR (°): classificação em graus (°), com destaque para encostas de APP (&gt; 45°).\n\n'
                'Os tiles são selecionados localmente pelo índice MGRS '
                '(sem consulta à internet) e armazenados em cache após o '
                'primeiro download, reduzindo o consumo de dados nas '
                'execuções seguintes.'
            )
        )

    def tr(self, string: str) -> str:
        return QCoreApplication.translate('Processing', string)

    def _preparar_declividade(
        self,
        merged_path: str,
        estilo_idx: int,
        declividade_categorica: bool,
        feedback: QgsProcessingFeedback
    ) -> QgsRasterLayer | None:
        """Calcula a declividade e gera o arquivo de estilo QML correspondente.

        Args:
            merged_path: Caminho para o MDE UTM mesclado.
            estilo_idx: Índice do estilo de declividade selecionado.
            declividade_categorica: Se True, o raster gerado será reclassificado fisicamente.
            feedback: Objeto de feedback para logs e progresso.

        Returns:
            QgsRasterLayer | None: Camada QgsRasterLayer configurada com a declividade ou None em caso de falha.
        """
        from pathlib import Path
        feedback.pushInfo('\nPreparando camada de Declividade...')

        as_percent = estilo_idx != 3  # Apenas o estilo CAR usa graus

        temp_dir_path = Path(self.temp_dir)
        slope_path = temp_dir_path / 'slope.tif'
        raw_slope_path = temp_dir_path / 'slope_raw.tif'

        # Remove arquivos anteriores se existirem
        for p in (slope_path, raw_slope_path):
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

        # Executar gdal.DEMProcessing
        unit_str = 'porcentagem' if as_percent else 'graus'
        feedback.pushInfo(f'  → Calculando declividade em {unit_str}...')

        # Se for categórica e estilo selecionado, a saída intermediária será slope_raw.tif
        usar_reclassificacao = declividade_categorica and estilo_idx > 0
        dem_proc_out = raw_slope_path if usar_reclassificacao else slope_path

        try:
            options = gdal.DEMProcessingOptions(
                slopeFormat='percent' if as_percent else 'degree',
                computeEdges=True
            )
            _ds = gdal.DEMProcessing(str(dem_proc_out), merged_path, 'slope', options=options)
            _ds = None  # Fecha o arquivo
        except Exception as e:
            feedback.pushInfo(f'  ❌ Erro ao calcular declividade via GDAL: {e}')
            return None

        if not dem_proc_out.exists():
            feedback.pushInfo('  ❌ Arquivo de declividade não foi gerado.')
            return None

        # Reclassificação categórica se ativada
        if usar_reclassificacao:
            feedback.pushInfo('  → Reclassificando raster de declividade para categórico...')
            try:
                if estilo_idx == 1:
                    # FAO: 10 classes
                    expr = (
                        "(A <= 0.2)*1 + ((A > 0.2) & (A <= 0.5))*2 + ((A > 0.5) & (A <= 1))*3 "
                        "+ ((A > 1) & (A <= 2))*4 + ((A > 2) & (A <= 5))*5 + ((A > 5) & (A <= 10))*6 "
                        "+ ((A > 10) & (A <= 15))*7 + ((A > 15) & (A <= 30))*8 + ((A > 30) & (A <= 60))*9 "
                        "+ (A > 60)*10"
                    )
                elif estilo_idx == 2:
                    # Embrapa: 6 classes
                    expr = (
                        "(A <= 3)*1 + ((A > 3) & (A <= 8))*2 + ((A > 8) & (A <= 20))*3 "
                        "+ ((A > 20) & (A <= 45))*4 + ((A > 45) & (A <= 75))*5 + (A > 75)*6"
                    )
                else:
                    # CAR: 3 classes
                    expr = "(A <= 25)*1 + ((A > 25) & (A <= 45))*2 + (A > 45)*3"

                Calc(
                    calc=expr,
                    A=str(raw_slope_path),
                    outfile=str(slope_path),
                    NoDataValue=-32768,
                    type='Byte',  # Salva como Byte/Inteiro de 8 bits para raster categórico leve
                    overwrite=True
                )
            except Exception as e:
                feedback.pushInfo(f'  ❌ Erro na reclassificação via gdal_calc: {e}')
                return None
            finally:
                if raw_slope_path.exists():
                    try:
                        raw_slope_path.unlink()
                    except Exception:
                        pass

        if not slope_path.exists():
            feedback.pushInfo('  ❌ Arquivo de declividade reclassificado não foi gerado.')
            return None

        # Aplicar estilo QML se selecionado
        estilo_nomes = [
            'Sem estilo',
            'FAO (%)',
            'Embrapa (%)',
            'CAR (°)'
        ]
        estilo_label = estilo_nomes[estilo_idx]

        qml_path = slope_path.with_suffix('.qml')

        # Se um estilo foi selecionado, escreve o arquivo QML e carrega na camada
        if estilo_idx > 0:
            qml_templates = {
                1: self._qml_slope_fao,
                2: self._qml_slope_embrapa,
                3: self._qml_slope_car
            }
            template_func = qml_templates.get(estilo_idx)
            if template_func:
                try:
                    qml_path.write_text(template_func(usar_reclassificacao), encoding='utf-8')
                except Exception as e:
                    feedback.pushInfo(f'  ⚠️ Erro ao gravar arquivo de estilo QML: {e}')

        slope_layer = QgsRasterLayer(str(slope_path), f'ANADEM v1 — Declividade ({estilo_label})')
        if not slope_layer.isValid():
            feedback.pushInfo('  ❌ Erro ao carregar camada de declividade no QGIS.')
            return None

        if estilo_idx > 0 and qml_path.exists():
            slope_layer.loadNamedStyle(str(qml_path))
            slope_layer.triggerRepaint()

        return slope_layer

    @staticmethod
    def _qml_slope_car(categorico: bool = False) -> str:
        """Retorna o template QML para o estilo CAR/Código Florestal (graus).

        Args:
            categorico: Se True, gera o QML com renderizador "paletted" (valores únicos).

        Returns:
            str: Conteúdo do arquivo de estilo QML formatado em XML.
        """
        if categorico:
            return (
                "<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n"
                "<qgis styleCategories=\"Symbology\" version=\"3.44.8-Solothurn\">\n"
                "  <pipe>\n"
                "    <provider>\n"
                "      <resampling zoomedOutResamplingMethod=\"nearestNeighbour\" maxOversampling=\"2\" enabled=\"false\" zoomedInResamplingMethod=\"nearestNeighbour\"/>\n"
                "    </provider>\n"
                "    <rasterrenderer nodataColor=\"\" alphaBand=\"-1\" band=\"1\" opacity=\"1\" type=\"paletted\">\n"
                "      <rasterTransparency/>\n"
                "      <colorPalette>\n"
                "        <paletteEntry label=\"0° – 25° (Baixa a moderada declividade)\" color=\"#1a9850\" alpha=\"255\" value=\"1\"/>\n"
                "        <paletteEntry label=\"25° – 45° (Alta declividade / atenção ambiental)\" color=\"#fee08b\" alpha=\"255\" value=\"2\"/>\n"
                "        <paletteEntry label=\"&gt; 45° (APP - Encosta com declividade superior a 45°)\" color=\"#d73027\" alpha=\"255\" value=\"3\"/>\n"
                "      </colorPalette>\n"
                "    </rasterrenderer>\n"
                "    <brightnesscontrast brightness=\"0\" gamma=\"1\" contrast=\"0\"/>\n"
                "    <huesaturation colorizeStrength=\"100\" invertColors=\"0\" colorizeBlue=\"128\" colorizeRed=\"255\" colorizeGreen=\"128\" colorizeOn=\"0\" grayscaleMode=\"0\" saturation=\"0\"/>\n"
                "    <rasterresampler maxOversampling=\"2\"/>\n"
                "  </pipe>\n"
                "  <blendMode>0</blendMode>\n"
                "</qgis>\n"
            )

        return (
            "<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n"
            "<qgis styleCategories=\"Symbology\" version=\"3.44.8-Solothurn\">\n"
            "  <pipe>\n"
            "    <provider>\n"
            "      <resampling zoomedOutResamplingMethod=\"nearestNeighbour\" maxOversampling=\"2\" enabled=\"false\" zoomedInResamplingMethod=\"nearestNeighbour\"/>\n"
            "    </provider>\n"
            "    <rasterrenderer nodataColor=\"\" alphaBand=\"-1\" band=\"1\" opacity=\"1\" type=\"singlebandpseudocolor\" classificationMax=\"90\" classificationMin=\"0\">\n"
            "      <rasterTransparency/>\n"
            "      <rastershader>\n"
            "        <colorrampshader minimumValue=\"0\" labelPrecision=\"2\" clip=\"0\" maximumValue=\"90\" colorRampType=\"DISCRETE\" classificationMode=\"2\">\n"
            "          <item label=\"0° – 25° (Baixa a moderada declividade)\" color=\"#1a9850\" alpha=\"255\" value=\"25\"/>\n"
            "          <item label=\"25° – 45° (Alta declividade / atenção ambiental)\" color=\"#fee08b\" alpha=\"255\" value=\"45\"/>\n"
            "          <item label=\"&gt; 45° (APP - Encosta com declividade superior a 45°)\" color=\"#d73027\" alpha=\"255\" value=\"inf\"/>\n"
            "        </colorrampshader>\n"
            "      </rastershader>\n"
            "    </rasterrenderer>\n"
            "    <brightnesscontrast brightness=\"0\" gamma=\"1\" contrast=\"0\"/>\n"
            "    <huesaturation colorizeStrength=\"100\" invertColors=\"0\" colorizeBlue=\"128\" colorizeRed=\"255\" colorizeGreen=\"128\" colorizeOn=\"0\" grayscaleMode=\"0\" saturation=\"0\"/>\n"
            "    <rasterresampler maxOversampling=\"2\"/>\n"
            "  </pipe>\n"
            "  <blendMode>0</blendMode>\n"
            "</qgis>\n"
        )

    @staticmethod
    def _qml_slope_embrapa(categorico: bool = False) -> str:
        """Retorna o template QML para o estilo Embrapa (porcentagem).

        Args:
            categorico: Se True, gera o QML com renderizador "paletted" (valores únicos).

        Returns:
            str: Conteúdo do arquivo de estilo QML formatado em XML.
        """
        if categorico:
            return (
                "<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n"
                "<qgis styleCategories=\"Symbology\" version=\"3.44.8-Solothurn\">\n"
                "  <pipe>\n"
                "    <provider>\n"
                "      <resampling zoomedOutResamplingMethod=\"nearestNeighbour\" maxOversampling=\"2\" enabled=\"false\" zoomedInResamplingMethod=\"nearestNeighbour\"/>\n"
                "    </provider>\n"
                "    <rasterrenderer nodataColor=\"\" alphaBand=\"-1\" band=\"1\" opacity=\"1\" type=\"paletted\">\n"
                "      <rasterTransparency/>\n"
                "      <colorPalette>\n"
                "        <paletteEntry label=\"0% – 3% (Plano)\" color=\"#286fa4\" alpha=\"255\" value=\"1\"/>\n"
                "        <paletteEntry label=\"3% - 8% (Suave ondulado)\" color=\"#a2f9d0\" alpha=\"255\" value=\"2\"/>\n"
                "        <paletteEntry label=\"8% - 20% (Ondulado)\" color=\"#4cc64d\" alpha=\"255\" value=\"3\"/>\n"
                "        <paletteEntry label=\"20% - 45% (Forte ondulado)\" color=\"#f1eb7a\" alpha=\"255\" value=\"4\"/>\n"
                "        <paletteEntry label=\"45% - 75% (Montanhoso)\" color=\"#ffae5d\" alpha=\"255\" value=\"5\"/>\n"
                "        <paletteEntry label=\"&gt; 75% (Escarpado)\" color=\"#b11011\" alpha=\"255\" value=\"6\"/>\n"
                "      </colorPalette>\n"
                "    </rasterrenderer>\n"
                "    <brightnesscontrast brightness=\"0\" gamma=\"1\" contrast=\"0\"/>\n"
                "    <huesaturation colorizeStrength=\"100\" invertColors=\"0\" colorizeBlue=\"128\" colorizeRed=\"255\" colorizeGreen=\"128\" colorizeOn=\"0\" grayscaleMode=\"0\" saturation=\"0\"/>\n"
                "    <rasterresampler maxOversampling=\"2\"/>\n"
                "  </pipe>\n"
                "  <blendMode>0</blendMode>\n"
                "</qgis>\n"
            )

        return (
            "<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n"
            "<qgis styleCategories=\"Symbology\" version=\"3.44.8-Solothurn\">\n"
            "  <pipe>\n"
            "    <provider>\n"
            "      <resampling zoomedOutResamplingMethod=\"nearestNeighbour\" maxOversampling=\"2\" enabled=\"false\" zoomedInResamplingMethod=\"nearestNeighbour\"/>\n"
            "    </provider>\n"
            "    <rasterrenderer nodataColor=\"\" alphaBand=\"-1\" band=\"1\" opacity=\"1\" type=\"singlebandpseudocolor\" classificationMax=\"900\" classificationMin=\"3\">\n"
            "      <rasterTransparency/>\n"
            "      <rastershader>\n"
            "        <colorrampshader minimumValue=\"3\" labelPrecision=\"4\" clip=\"0\" maximumValue=\"500\" colorRampType=\"DISCRETE\" classificationMode=\"2\">\n"
            "          <item label=\"0% – 3% (Plano)\" color=\"#286fa4\" alpha=\"255\" value=\"3\"/>\n"
            "          <item label=\"3% - 8% (Suave ondulado)\" color=\"#a2f9d0\" alpha=\"255\" value=\"8\"/>\n"
            "          <item label=\"8% - 20% (Ondulado)\" color=\"#4cc64d\" alpha=\"255\" value=\"20\"/>\n"
            "          <item label=\"20% - 45% (Forte ondulado)\" color=\"#f1eb7a\" alpha=\"255\" value=\"45\"/>\n"
            "          <item label=\"45% - 75% (Montanhoso)\" color=\"#ffae5d\" alpha=\"255\" value=\"75\"/>\n"
            "          <item label=\"&gt; 75% (Escarpado)\" color=\"#b11011\" alpha=\"255\" value=\"inf\"/>\n"
            "        </colorrampshader>\n"
            "      </rastershader>\n"
            "    </rasterrenderer>\n"
            "    <brightnesscontrast brightness=\"0\" gamma=\"1\" contrast=\"0\"/>\n"
            "    <huesaturation colorizeStrength=\"100\" invertColors=\"0\" colorizeBlue=\"128\" colorizeRed=\"255\" colorizeGreen=\"128\" colorizeOn=\"0\" grayscaleMode=\"0\" saturation=\"0\"/>\n"
            "    <rasterresampler maxOversampling=\"2\"/>\n"
            "  </pipe>\n"
            "  <blendMode>0</blendMode>\n"
            "</qgis>\n"
        )

    @staticmethod
    def _qml_slope_fao(categorico: bool = False) -> str:
        """Retorna o template QML para o estilo FAO (porcentagem).

        Args:
            categorico: Se True, gera o QML com renderizador "paletted" (valores únicos).

        Returns:
            str: Conteúdo do arquivo de estilo QML formatado em XML.
        """
        if categorico:
            return (
                "<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n"
                "<qgis styleCategories=\"Symbology\" version=\"3.44.8-Solothurn\">\n"
                "  <pipe>\n"
                "    <provider>\n"
                "      <resampling zoomedOutResamplingMethod=\"nearestNeighbour\" maxOversampling=\"2\" enabled=\"false\" zoomedInResamplingMethod=\"nearestNeighbour\"/>\n"
                "    </provider>\n"
                "    <rasterrenderer nodataColor=\"\" alphaBand=\"-1\" band=\"1\" opacity=\"1\" type=\"paletted\">\n"
                "      <rasterTransparency/>\n"
                "      <colorPalette>\n"
                "        <paletteEntry label=\"0% – 0.2% (Flat)\" color=\"#82a4ff\" alpha=\"255\" value=\"1\"/>\n"
                "        <paletteEntry label=\"0.2% – 0.5% (Level)\" color=\"#66bd63\" alpha=\"255\" value=\"2\"/>\n"
                "        <paletteEntry label=\"0.5% – 1.0% (Nearly level)\" color=\"#a6d96a\" alpha=\"255\" value=\"3\"/>\n"
                "        <paletteEntry label=\"1.0% – 2.0% (Very gently sloping)\" color=\"#d9ef8b\" alpha=\"255\" value=\"4\"/>\n"
                "        <paletteEntry label=\"2% – 5% (Gently sloping)\" color=\"#ffffbf\" alpha=\"255\" value=\"5\"/>\n"
                "        <paletteEntry label=\"5% – 10% (Sloping)\" color=\"#fee08b\" alpha=\"255\" value=\"6\"/>\n"
                "        <paletteEntry label=\"10% – 15% (Strongly sloping)\" color=\"#fdae61\" alpha=\"255\" value=\"7\"/>\n"
                "        <paletteEntry label=\"15% – 30% (Moderately steep)\" color=\"#f46d43\" alpha=\"255\" value=\"8\"/>\n"
                "        <paletteEntry label=\"30% – 60% (Steep)\" color=\"#d73027\" alpha=\"255\" value=\"9\"/>\n"
                "        <paletteEntry label=\"&gt; 60% (Very steep)\" color=\"#7f0000\" alpha=\"255\" value=\"10\"/>\n"
                "      </colorPalette>\n"
                "    </rasterrenderer>\n"
                "    <brightnesscontrast brightness=\"0\" gamma=\"1\" contrast=\"0\"/>\n"
                "    <huesaturation colorizeStrength=\"100\" invertColors=\"0\" colorizeBlue=\"128\" colorizeRed=\"255\" colorizeGreen=\"128\" colorizeOn=\"0\" grayscaleMode=\"0\" saturation=\"0\"/>\n"
                "    <rasterresampler maxOversampling=\"2\"/>\n"
                "  </pipe>\n"
                "  <blendMode>0</blendMode>\n"
                "</qgis>\n"
            )

        return (
            "<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n"
            "<qgis styleCategories=\"Symbology\" version=\"3.44.8-Solothurn\">\n"
            "  <pipe>\n"
            "    <provider>\n"
            "      <resampling zoomedOutResamplingMethod=\"nearestNeighbour\" maxOversampling=\"2\" enabled=\"false\" zoomedInResamplingMethod=\"nearestNeighbour\"/>\n"
            "    </provider>\n"
            "    <rasterrenderer nodataColor=\"\" alphaBand=\"-1\" band=\"1\" opacity=\"1\" type=\"singlebandpseudocolor\" classificationMax=\"60\" classificationMin=\"0\">\n"
            "      <rasterTransparency/>\n"
            "      <rastershader>\n"
            "        <colorrampshader minimumValue=\"0\" labelPrecision=\"4\" clip=\"0\" maximumValue=\"60\" colorRampType=\"DISCRETE\" classificationMode=\"2\">\n"
            "          <item label=\"0% – 0.2% (Flat)\" color=\"#82a4ff\" alpha=\"255\" value=\"0.2\"/>\n"
            "          <item label=\"0.2% – 0.5% (Level)\" color=\"#66bd63\" alpha=\"255\" value=\"0.5\"/>\n"
            "          <item label=\"0.5% – 1.0% (Nearly level)\" color=\"#a6d96a\" alpha=\"255\" value=\"1\"/>\n"
            "          <item label=\"1.0% – 2.0% (Very gently sloping)\" color=\"#d9ef8b\" alpha=\"255\" value=\"2\"/>\n"
            "          <item label=\"2% – 5% (Gently sloping)\" color=\"#ffffbf\" alpha=\"255\" value=\"5\"/>\n"
            "          <item label=\"5% – 10% (Sloping)\" color=\"#fee08b\" alpha=\"255\" value=\"10\"/>\n"
            "          <item label=\"10% – 15% (Strongly sloping)\" color=\"#fdae61\" alpha=\"255\" value=\"15\"/>\n"
            "          <item label=\"15% – 30% (Moderately steep)\" color=\"#f46d43\" alpha=\"255\" value=\"30\"/>\n"
            "          <item label=\"30% – 60% (Steep)\" color=\"#d73027\" alpha=\"255\" value=\"60\"/>\n"
            "          <item label=\"&gt; 60% (Very steep)\" color=\"#7f0000\" alpha=\"255\" value=\"inf\"/>\n"
            "        </colorrampshader>\n"
            "      </rastershader>\n"
            "    </rasterrenderer>\n"
            "    <brightnesscontrast brightness=\"0\" gamma=\"1\" contrast=\"0\"/>\n"
            "    <huesaturation colorizeStrength=\"100\" invertColors=\"0\" colorizeBlue=\"128\" colorizeRed=\"255\" colorizeGreen=\"128\" colorizeOn=\"0\" grayscaleMode=\"0\" saturation=\"0\"/>\n"
            "    <rasterresampler maxOversampling=\"2\"/>\n"
            "  </pipe>\n"
            "  <blendMode>0</blendMode>\n"
            "</qgis>\n"
        )

    def createInstance(self):
        return FluxoTopograficoAlgorithm()
