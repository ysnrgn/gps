"""
mapping.py - Folium/OpenSeaMap HTML map generation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Dict, List, Optional, Tuple


@dataclass
class MarkerStyle:
    """Map marker visual configuration."""

    color: str
    icon: str
    label: str


SOURCE_MARKER = MarkerStyle(color="blue", icon="info-sign", label="Kaynak")
RAW_MID_MARKER = MarkerStyle(color="orange", icon="star", label="Ham Orta Nokta")
FILTERED_MID_MARKER = MarkerStyle(color="red", icon="star", label="Filtreli Orta Nokta")


class LiveMap:
    """Generate a Folium map with OpenSeaMap tiles and GNSS markers."""

    def __init__(self, initial_center: Tuple[float, float] = (39.9, 32.8), zoom: int = 22) -> None:
        self._center = initial_center
        self.zoom = int(zoom)
        self._sources: Dict[str, Tuple[float, float]] = {}
        self._raw_midpoint: Optional[Tuple[float, float]] = None
        self._filtered_midpoint: Optional[Tuple[float, float]] = None
        self._map = None
        self._offline_layers: List[Dict[str, object]] = []
        self._primary_source_id: str = ""
        self._primary_target_id: str = ""

    def set_primary_pair(self, source_id: str, target_id: str) -> None:
        self._primary_source_id = source_id
        self._primary_target_id = target_id

    def set_offline_layers(self, layers: List[Dict[str, object]]) -> None:
        """Set local tile layers served from MBTiles cache."""
        self._offline_layers = list(layers)

    def initialize_map(self) -> None:
        """Create the internal Folium map object."""
        self._map = self._build_map()

    def update_source_markers(self, sources: Dict[str, Dict]) -> None:
        """Update blue source station markers."""
        self._sources = dict(sources)
        if sources:
            lat = sum(v["lat"] for v in sources.values()) / len(sources)
            lon = sum(v["lon"] for v in sources.values()) / len(sources)
            self._center = (lat, lon)

    def update_raw_midpoint(self, latitude: float, longitude: float) -> None:
        """Update the orange raw midpoint marker."""
        self._raw_midpoint = (latitude, longitude)
        self._center = (latitude, longitude)

    def update_filtered_midpoint(self, latitude: float, longitude: float) -> None:
        """Update the red filtered midpoint marker."""
        self._filtered_midpoint = (latitude, longitude)
        self._center = (latitude, longitude)

    def render_to_html(self, output_path: str) -> str:
        """Render the current map state to an HTML file."""
        folium_map = self._build_map()
        output = Path(output_path).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        folium_map.save(str(output))
        self._map = folium_map
        return str(output)

    def clear_all_markers(self) -> None:
        """Clear dynamic markers."""
        self._sources.clear()
        self._raw_midpoint = None
        self._filtered_midpoint = None

    @property
    def center(self) -> Tuple[float, float]:
        """Return the current map center."""
        return self._center

    def _build_map(self):
        try:
            import folium
        except ImportError as exc:
            raise RuntimeError("folium is required for map rendering. Install with: pip install folium") from exc

        folium_map = folium.Map(
            location=self._center,
            zoom_start=self.zoom,
            tiles=None,
            max_zoom=25,
            control_scale=True,
        )

        if self._offline_layers:
            for idx, layer in enumerate(self._offline_layers):
                folium.TileLayer(
                    tiles=str(layer["url"]),
                    attr=str(layer.get("attribution", "Offline MBTiles")),
                    name=str(layer["name"]),
                    overlay=bool(layer.get("overlay", False)),
                    max_zoom=int(layer.get("max_zoom", 22)),
                    max_native_zoom=int(layer.get("max_native_zoom", 11)),
                    control=True,
                    show=idx == 0 or bool(layer.get("show", False)),
                ).add_to(folium_map)
            # Online OSM her zaman yedek olarak ekle (başlangıçta gizli)
            folium.TileLayer(
                tiles="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
                attr="© OpenStreetMap contributors",
                name="Online OSM",
                overlay=False,
                max_zoom=25,
                max_native_zoom=19,
                control=True,
                show=False,
            ).add_to(folium_map)
        else:
            # OpenStreetMap taban — tek base layer (max_native_zoom=19, overzoom ile 22'ye kadar)
            folium.TileLayer(
                tiles="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
                attr="OpenStreetMap contributors",
                name="OpenStreetMap",
                overlay=False,
                max_zoom=25,
                max_native_zoom=19,
                control=True,
                show=True,
            ).add_to(folium_map)

            # OpenSeaMap deniz işaretleri: fener, şamandıra, liman vb.
            folium.TileLayer(
                tiles="https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png",
                attr="OpenSeaMap contributors",
                name="Sea Marks",
                overlay=True,
                max_zoom=25,
                max_native_zoom=18,
                control=True,
                show=True,
            ).add_to(folium_map)

            # Marine Profile: GEBCO 2021 derinlik renklendirmesi
            folium.raster_layers.WmsTileLayer(
                url="https://geoserver.openseamap.org/geoserver/wms",
                layers="gebco2021:gebco_2021",
                name="Marine Profile",
                fmt="image/png",
                transparent=True,
                overlay=True,
                control=True,
                show=True,
                attr="OpenSeaMap / GEBCO 2021",
            ).add_to(folium_map)

            # Derinlik konturları (GEBCO 2021 kontur çizgileri)
            folium.raster_layers.WmsTileLayer(
                url="https://geoserver.openseamap.org/geoserver/wms",
                layers="gebco2021:gebco2021_contour",
                name="Depth Contours",
                fmt="image/png",
                transparent=True,
                overlay=True,
                control=True,
                show=True,
                attr="OpenSeaMap / GEBCO 2021 contours",
            ).add_to(folium_map)

        # Dynamic GNSS markers are injected with get_update_js() after the page
        # loads. Keeping the base HTML marker-free prevents default Leaflet
        # popups from appearing before the command-center overlay style loads.

        folium.LayerControl().add_to(folium_map)

        # Leaflet harita nesnesini window._gnssMap olarak sakla (JS enjeksiyonu için)
        expose = folium.Element("""
<style>
.gnss-map-label {
    color: #f8fafc;
    font: 800 13px Arial, sans-serif;
    text-shadow: 0 1px 3px #000, 0 0 6px #000;
    white-space: nowrap;
}
.gnss-center-label {
    color: #fde047;
    font: 900 12px Arial, sans-serif;
    text-align: center;
    text-shadow: 0 1px 3px #000, 0 0 7px #000;
    white-space: nowrap;
    min-width: 100px;
}
.gnss-popup-card {
    background: linear-gradient(180deg, rgba(37, 50, 62, 0.97), rgba(20, 28, 37, 0.98));
    border: 1px solid rgba(93, 111, 129, 0.86);
    border-radius: 5px;
    box-shadow: 0 10px 24px rgba(0, 0, 0, 0.62);
    color: #f8fafc;
    font: 16px Arial, sans-serif;
    min-width: 246px;
    overflow: hidden;
}
.gnss-popup-title {
    align-items: center;
    background: rgba(45, 59, 72, 0.96);
    border-bottom: 1px solid rgba(93, 111, 129, 0.7);
    cursor: grab;
    display: flex;
    font-size: 23px;
    font-weight: 800;
    justify-content: space-between;
    letter-spacing: .2px;
    padding: 8px 14px;
    text-transform: uppercase;
    user-select: none;
}
.gnss-popup-close {
    color: #cbd5e1;
    cursor: pointer;
    font-size: 32px;
    font-weight: 400;
    line-height: 1;
}
.gnss-popup-body {
    padding: 8px 14px 12px;
}
.gnss-popup-row {
    display: grid;
    grid-template-columns: 105px 1fr;
    line-height: 1.24;
}
.gnss-popup-key {
    color: #cbd5e1;
}
.gnss-popup-val {
    color: #ffffff;
    font-weight: 700;
    text-align: right;
}
.gnss-popup-foot {
    align-items: center;
    border-top: 1px solid rgba(93, 111, 129, 0.48);
    display: flex;
    gap: 12px;
    justify-content: flex-end;
    margin-top: 12px;
    padding-top: 10px;
}
.gnss-popup-btn {
    background: linear-gradient(180deg, #2e3c4b, #202b36);
    border: 1px solid #556577;
    border-radius: 6px;
    color: #f8fafc;
    cursor: pointer;
    font-size: 18px;
    padding: 9px 17px;
    white-space: nowrap;
}
.gnss-popup-btn:hover {
    background: #394a5b;
}
.gnss-flag-form-card {
    background: linear-gradient(180deg, rgba(37, 50, 62, 0.98), rgba(18, 25, 33, 0.99));
    border: 1px solid rgba(93, 111, 129, 0.9);
    border-radius: 5px;
    box-shadow: 0 12px 28px rgba(0, 0, 0, 0.64);
    color: #f8fafc;
    font: 14px Arial, sans-serif;
    min-width: 260px;
    overflow: hidden;
}
.gnss-flag-form-body {
    display: grid;
    gap: 7px;
    padding: 10px 12px 12px;
}
.gnss-flag-form-body label {
    color: #cbd5e1;
    display: grid;
    gap: 3px;
    font-size: 12px;
}
.gnss-flag-form-body input {
    background: #111827;
    border: 1px solid #435468;
    border-radius: 4px;
    color: #f8fafc;
    font: 13px Arial, sans-serif;
    padding: 7px 8px;
}
.gnss-flag-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    padding-top: 5px;
}
.leaflet-popup-content-wrapper,
.leaflet-popup-tip {
    background: transparent !important;
    box-shadow: none !important;
}
.leaflet-popup-content {
    margin: 0 !important;
}
.leaflet-popup-close-button {
    display: none !important;
}
.leaflet-control-layers {
    z-index: 5000 !important;
}
.gnss-flag-icon {
    color: #ef4444;
    font: 900 30px Arial, sans-serif;
    text-shadow: 0 1px 3px #000, 0 0 5px #000;
}
.gnss-heading-arrow {
    background: transparent !important;
    border: none !important;
}
.gnss-measure-label div,
.gnss-coord-label div {
    background: rgba(15, 23, 32, 0.92);
    border: 1px solid #facc15;
    border-radius: 4px;
    color: #facc15;
    font: 800 13px Arial, sans-serif;
    padding: 4px 8px;
    white-space: nowrap;
}
.gnss-ruler-panel {
    background: rgba(15, 23, 32, 0.94);
    border: 1px solid #facc15;
    border-radius: 4px;
    color: #f8fafc;
    font: 12px Arial, sans-serif;
    padding: 7px 10px;
    pointer-events: none;
    white-space: nowrap;
}
.gnss-ruler-panel strong {
    color: #facc15;
    display: block;
    font-size: 13px;
    margin-bottom: 2px;
}
.gnss-target-icon {
    align-items: center;
    background: rgba(244, 63, 94, 0.18);
    border: 3px solid #f43f5e;
    border-radius: 50%;
    box-shadow: 0 0 0 2px rgba(15, 23, 42, 0.9), 0 0 14px rgba(244, 63, 94, 0.72);
    color: #fff;
    display: flex;
    font: 900 18px Arial, sans-serif;
    height: 28px;
    justify-content: center;
    position: relative;
    width: 28px;
}
.gnss-target-icon::after {
    content: "";
    background: #f43f5e;
    height: 10px;
    left: 50%;
    position: absolute;
    top: 50%;
    transform: translate(-50%, -50%);
    width: 3px;
}
.gnss-target-icon::before {
    content: "";
    background: #f43f5e;
    height: 3px;
    left: 50%;
    position: absolute;
    top: 50%;
    transform: translate(-50%, -50%);
    width: 10px;
}
</style>
<script>
(function waitMap() {
    var found = Object.values(window).find(function(v) {
        return v && v._leaflet_id !== undefined && typeof v.getCenter === 'function';
    });
    if (found) {
        window._gnssMap = found;
        window._gnssMarkers = [];
    } else {
        setTimeout(waitMap, 100);
    }
})();
</script>""")
        folium_map.get_root().html.add_child(expose)
        if self._offline_layers:
            _ourl = str(self._offline_layers[0]["url"])
            _omax = int(str(self._offline_layers[0].get("max_native_zoom", 15)))
            folium_map.get_root().html.add_child(folium.Element(  # type: ignore[attr-defined]
                f"<script>window._gnssOfflineTileUrl='{_ourl}';"
                f"window._gnssOfflineMaxNative={_omax};</script>"
            ))
        return folium_map

    def get_update_js(
        self,
        pan: bool = True,
        device_meta: Optional[dict] = None,
        coord_format: str = "DD",
        show_baseline: bool = True,
    ) -> str:
        """Marker güncelleme JS'i döndürür — sayfa yenilenmez, zoom korunur."""
        if device_meta is None:
            device_meta = {}
        source_items = list(self._sources.items())
        lines = [
            "(function() { try {",
            "var m = window._gnssMap; if (!m) return;",
            f"window._gnssCoordFmt = '{coord_format}'; var coordFmt = window._gnssCoordFmt;",
            "if(!window._gnssSwitchMap){"
            " window._gnssMapLayerCache={};"
            " m.eachLayer(function(l){"
            "  if(l._url&&!(l.options&&l.options.overlay)){"
            "   if(l._url.indexOf('openstreetmap.org')>-1)window._gnssMapLayerCache['online']=l;"
            "   else window._gnssMapLayerCache['offline']=l;"
            "   window._gnssActiveBase=l;"
            "  }"
            " });"
            " window._gnssSwitchMap=function(type){"
            "  if(window._gnssActiveBase&&m.hasLayer(window._gnssActiveBase))m.removeLayer(window._gnssActiveBase);"
            "  if(!window._gnssMapLayerCache[type]){"
            "   if(type==='online')window._gnssMapLayerCache['online']=L.tileLayer("
            "    'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',"
            "    {maxZoom:22,maxNativeZoom:19,attribution:'&copy; OSM'});"
            "   else if(window._gnssOfflineTileUrl)window._gnssMapLayerCache['offline']=L.tileLayer("
            "    window._gnssOfflineTileUrl,"
            "    {maxZoom:22,maxNativeZoom:window._gnssOfflineMaxNative||15,attribution:'Offline'});"
            "  }"
            "  var layer=window._gnssMapLayerCache[type];"
            "  if(layer){layer.addTo(m);window._gnssActiveBase=layer;}"
            "  if(!window._gnssOnlineOverlays){"
            "   window._gnssOnlineOverlays=["
            "    L.tileLayer('https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png',"
            "     {maxZoom:25,maxNativeZoom:18,opacity:1,attribution:'OpenSeaMap'}),"
            "    L.tileLayer.wms('https://geoserver.openseamap.org/geoserver/wms',"
            "     {layers:'gebco2021:gebco_2021',format:'image/png',transparent:true,attribution:'GEBCO 2021',opacity:0.85}),"
            "    L.tileLayer.wms('https://geoserver.openseamap.org/geoserver/wms',"
            "     {layers:'gebco2021:gebco2021_contour',format:'image/png',transparent:true,attribution:'GEBCO 2021 contours',opacity:0.85})"
            "   ];"
            "  }"
            "  window._gnssOnlineOverlays.forEach(function(ol){"
            "   if(type==='online'){if(!m.hasLayer(ol))ol.addTo(m);}"
            "   else{if(m.hasLayer(ol))m.removeLayer(ol);}"
            "  });"
            " };"
            "}",
            "if (window._gnssMarkers) {"
            " window._gnssMarkers.forEach(function(mk){ try { m.removeLayer(mk); } catch(e){} }); }",
            "window._gnssMarkers = [];",
            "window._gnssPmk = window._gnssPmk || {};",
            "var _activePmk = {};",
            "function esc(v) { return String(v).replace(/'/g, '&#39;'); }",
            "function labelIcon(text, cls, anc) { return L.divIcon({className: cls, html: text, iconSize: null, iconAnchor: anc||[0,0]}); }",
            "function flagIcon() { return L.divIcon({className:'gnss-flag-icon',html:'⚑',iconSize:[28,28],iconAnchor:[6,24]}); }",
            "function hemi(v, axis) { return v >= 0 ? (axis === 'lat' ? 'N' : 'E') : (axis === 'lat' ? 'S' : 'W'); }",
            "function fmtCoord(v, axis) {"
            " var a = Math.abs(v), h = hemi(v, axis), d = Math.floor(a), mf = (a - d) * 60, mnt = Math.floor(mf), s = (mf - mnt) * 60;"
            " var dw = axis === 'lat' ? 2 : 3;"
            " var ds = String(d).padStart(dw, '0');"
            " if (coordFmt === 'DDM') return ds + '° ' + mf.toFixed(4).padStart(7, '0') + \"' \" + h;"
            " if (coordFmt === 'DMS') return ds + '° ' + String(mnt).padStart(2, '0') + \"' \" + s.toFixed(2).padStart(5, '0') + '\" ' + h;"
            " if (coordFmt === 'NMEA') { var base = d * 100 + mf; return base.toFixed(4).padStart(axis === 'lat' ? 7 : 8, '0') + ' ' + h; }"
            " return a.toFixed(7) + '° ' + h;"
            "}"
            " window._gnssFmtCoord = fmtCoord;",
            "if(window._gnssUserFlagData&&window._gnssUserFlags){"
            " window._gnssUserFlagData.forEach(function(d,i){"
            "  var mk=window._gnssUserFlags[i*2];"
            "  if(mk&&mk.getPopup&&mk.getPopup()){mk.getPopup().setContent(card(d.name,d.lat,d.lon,d.alt||'-',null,null,null,false));}"
            " });"
            "}",
            "if(window._gnssTargetData&&window._gnssTargets){"
            " window._gnssTargetData.forEach(function(d,i){"
            "  var mk=window._gnssTargets[i*2];"
            "  if(mk&&mk.getPopup&&mk.getPopup()){mk.getPopup().setContent(card(d.name,d.lat,d.lon,d.alt||'-',null,null,null,false));}"
            " });"
            "}",
            "if(!window._gnssLegend){"
            " var _lgnd=L.control({position:'bottomleft'});"
            " _lgnd.onAdd=function(){"
            "  var d=L.DomUtil.create('div');"
            "  d.style.cssText='background:rgba(15,23,32,0.88);border:1px solid #374151;"
            "border-radius:6px;padding:8px 12px;font:11px/1.8 Arial,sans-serif;color:#e5e7eb;"
            "pointer-events:none;min-width:180px;';"
            "  d.innerHTML="
            "   '<b style=\"color:#94a3b8;font-size:10px;letter-spacing:.05em\">LEJANT</b><br>'"
            "   +'<span style=\"color:#0ea5e9\">━━</span> GPS Baseline<br>'"
            "   +'<span style=\"color:#ef4444\">●</span> Center Point (CP-01)<br>'"
            "   +'<span style=\"color:#facc15\">●</span> Center Point (Ham)<br>'"
            "   +'<span style=\"color:#38bdf8\">●</span> GPS Cihazı<br>'"
            "   +'<span style=\"color:#a78bfa\">━━</span> Ölçüm Çizgisi<br>'"
            "   +'<span style=\"color:#10b981\">⚑</span> Bayrak Noktası<br>'"
            "   +'<span style=\"color:#f97316\">◎</span> Hedef Noktası';"
            "  return d;};"
            " _lgnd.addTo(m);"
            " window._gnssLegend=_lgnd;"
            "}",
            "if (!window._gnssCoordGridDefined) {"
            " window._gnssCoordGridDefined = true;"
            " var _CGL = L.GridLayer.extend({"
            "  createTile: function(coords) {"
            "   var t = document.createElement('canvas');"
            "   var sz = this.getTileSize();"
            "   t.width = sz.x; t.height = sz.y;"
            "   var ctx = t.getContext('2d');"
            "   ctx.fillStyle = '#0d1117';"
            "   ctx.fillRect(0, 0, sz.x, sz.y);"
            "   ctx.strokeStyle = '#1e2d3d'; ctx.lineWidth = 0.8;"
            "   ctx.beginPath();"
            "   var step = sz.x / 4;"
            "   for (var x=step; x<sz.x; x+=step){ ctx.moveTo(x,0); ctx.lineTo(x,sz.y); }"
            "   for (var y=step; y<sz.y; y+=step){ ctx.moveTo(0,y); ctx.lineTo(sz.x,y); }"
            "   ctx.stroke();"
            "   return t;"
            "  }"
            " });"
            " window._gnssCoordGrid = new _CGL({opacity:1, zIndex:0});"
            " window._gnssCoordGridActive = false;"
            " window._gnssToggleCoordGrid = function() {"
            "  var cont = document.querySelector('.leaflet-container');"
            "  if (!window._gnssCoordGridActive) {"
            "   window._gnssHiddenTileLayers = [];"
            "   m.eachLayer(function(l){ if(l._url){ window._gnssHiddenTileLayers.push(l); try{m.removeLayer(l);}catch(e){} } });"
            "   if (cont) cont.style.background = '#0d1117';"
            "   window._gnssCoordGrid.addTo(m);"
            "   window._gnssCoordGridActive = true;"
            "  } else {"
            "   try{m.removeLayer(window._gnssCoordGrid);}catch(e){}"
            "   (window._gnssHiddenTileLayers||[]).forEach(function(l){ try{l.addTo(m);}catch(e){} });"
            "   window._gnssHiddenTileLayers = [];"
            "   if (cont) cont.style.background = '';"
            "   window._gnssCoordGridActive = false;"
            "  }"
            " };"
            "}",
            "window.gnssEnsureSelectableTools=function(m){"
            " if(window._gnssSelectableReady)return;"
            " window._gnssSelectableReady=true;"
            " window._gnssSelectableGroups=[];"
            " window._gnssSelectedGroup=null;"
            " window._gnssMultiSelect=[];"
            " window._gnssMeasureLine=null;"
            " window._gnssMeasureLabel=null;"
            " window._gnssLivePairs=window._gnssLivePairs||[];"
            " if(m.getContainer()){m.getContainer().tabIndex=0;}"
            " window._gnssSelectGroup=function(group){"
            "  try{m.getContainer().focus();}catch(e){}"
            "  if(window._gnssSelectedGroup&&window._gnssSelectedGroup!==group){"
            "   window._gnssSelectedGroup.layers.forEach(function(l){try{if(l.setStyle&&l._gnssDefaultStyle){l.setStyle(l._gnssDefaultStyle);}}catch(e){}});"
            "  }"
            "  window._gnssSelectedGroup=group;"
            "  group.layers.forEach(function(l){try{if(l.setStyle){l.setStyle({color:'#ef4444',fillColor:'#ef4444',weight:5,opacity:1,fillOpacity:1});}}catch(e){}});"
            " };"
            " window._gnssDeletedRuntime=window._gnssDeletedRuntime||{};"
            " window._gnssHighlightMulti=function(group){"
            "  group.layers.forEach(function(l){try{if(l.setStyle){l.setStyle({color:'#a78bfa',fillColor:'#7c3aed',weight:5,opacity:1,fillOpacity:1});}}catch(e){}});"
            " };"
            " window._gnssRestoreStyle=function(group){"
            "  group.layers.forEach(function(l){try{if(l.setStyle&&l._gnssDefaultStyle){l.setStyle(l._gnssDefaultStyle);}}catch(e){}});"
            " };"
            " window._gnssRemoveMeasure=function(){"
            "  if(window._gnssMeasureLine){try{m.removeLayer(window._gnssMeasureLine);}catch(e){}window._gnssMeasureLine=null;}"
            "  if(window._gnssMeasureLabel){try{m.removeLayer(window._gnssMeasureLabel);}catch(e){}window._gnssMeasureLabel=null;}"
            "  (window._gnssLivePairs||[]).forEach(function(p){"
            "   try{if(p.line)m.removeLayer(p.line);}catch(e){}"
            "   try{if(p.label)m.removeLayer(p.label);}catch(e){}"
            "  });"
            "  window._gnssLivePairs=[];"
            " };"
            " window._gnssGetMarkerLatLng=function(key){"
            "  if(!key)return null;"
            "  if(window._gnssPmk&&window._gnssPmk[key]){var mk=window._gnssPmk[key];if(mk.getLatLng)return mk.getLatLng();}"
            "  var gs=window._gnssSelectableGroups||[];"
            "  for(var i=0;i<gs.length;i++){if(gs[i].key===key){var l=gs[i].layers[0];if(l&&l.getLatLng)return l.getLatLng();if(l&&l.getBounds)return l.getBounds().getCenter();}}"
            "  return null;"
            " };"
            " window._gnssLiveUpdateMeasure=function(){"
            "  if(!window._gnssMultiSelect||window._gnssMultiSelect.length<2)return;"
            "  var aKey=window._gnssMultiSelect[0].key,bKey=window._gnssMultiSelect[1].key;"
            "  var aLL=window._gnssGetMarkerLatLng(aKey),bLL=window._gnssGetMarkerLatLng(bKey);"
            "  if(!aLL||!bLL)return;"
            "  if(window._gnssMeasureLine){window._gnssMeasureLine.setLatLngs([[aLL.lat,aLL.lng],[bLL.lat,bLL.lng]]);}else{"
            "   window._gnssMeasureLine=L.polyline([[aLL.lat,aLL.lng],[bLL.lat,bLL.lng]],{color:'#a78bfa',weight:3,dashArray:'8 5',opacity:0.95,interactive:false}).addTo(m);}"
            "  var R=6371000,dLat=(bLL.lat-aLL.lat)*Math.PI/180,dLon=(bLL.lng-aLL.lng)*Math.PI/180;"
            "  var si=Math.sin(dLat/2),sj=Math.sin(dLon/2);"
            "  var c=si*si+Math.cos(aLL.lat*Math.PI/180)*Math.cos(bLL.lat*Math.PI/180)*sj*sj;"
            "  var dist=R*2*Math.atan2(Math.sqrt(c),Math.sqrt(1-c));"
            "  var lbl=dist<1000?dist.toFixed(2)+' m':(dist/1000).toFixed(4)+' km';"
            "  var mid=L.latLng((aLL.lat+bLL.lat)/2,(aLL.lng+bLL.lng)/2);"
            "  var html=\"<div style='background:rgba(30,20,50,0.9);border:1.5px solid #a78bfa;border-radius:6px;padding:3px 10px;color:#e9d5ff;font-weight:700;font-size:13px;white-space:nowrap;box-shadow:0 2px 8px #0006'>&#128207; \"+lbl+\"</div>\";"
            "  var icon=L.divIcon({className:'',html:html,iconSize:null,iconAnchor:[50,14]});"
            "  if(window._gnssMeasureLabel){window._gnssMeasureLabel.setLatLng(mid).setIcon(icon);}else{"
            "   window._gnssMeasureLabel=L.marker(mid,{icon:icon,zIndexOffset:2000,interactive:false}).addTo(m);}"
            " };"
            " window._gnssRefreshLivePairs=function(){"
            "  (window._gnssLivePairs||[]).forEach(function(pair){"
            "   var aLL=window._gnssGetMarkerLatLng(pair.aKey),bLL=window._gnssGetMarkerLatLng(pair.bKey);"
            "   if(!aLL||!bLL)return;"
            "   if(pair.line){pair.line.setLatLngs([[aLL.lat,aLL.lng],[bLL.lat,bLL.lng]]);}else{"
            "    pair.line=L.polyline([[aLL.lat,aLL.lng],[bLL.lat,bLL.lng]],{color:'#a78bfa',weight:3,dashArray:'8 5',opacity:0.95,interactive:false}).addTo(m);}"
            "   var R=6371000,dLat=(bLL.lat-aLL.lat)*Math.PI/180,dLon=(bLL.lng-aLL.lng)*Math.PI/180;"
            "   var si=Math.sin(dLat/2),sj=Math.sin(dLon/2);"
            "   var c=si*si+Math.cos(aLL.lat*Math.PI/180)*Math.cos(bLL.lat*Math.PI/180)*sj*sj;"
            "   var dist=R*2*Math.atan2(Math.sqrt(c),Math.sqrt(1-c));"
            "   var lbl=dist<1000?dist.toFixed(2)+' m':(dist/1000).toFixed(4)+' km';"
            "   var mid=L.latLng((aLL.lat+bLL.lat)/2,(aLL.lng+bLL.lng)/2);"
            "   var html=\"<div style='background:rgba(30,20,50,0.9);border:1.5px solid #a78bfa;border-radius:6px;padding:3px 10px;color:#e9d5ff;font-weight:700;font-size:13px;white-space:nowrap;box-shadow:0 2px 8px #0006'>&#128207; \"+lbl+\"</div>\";"
            "   var icon=L.divIcon({className:'',html:html,iconSize:null,iconAnchor:[50,14]});"
            "   if(pair.label){pair.label.setLatLng(mid).setIcon(icon);}else{"
            "    pair.label=L.marker(mid,{icon:icon,zIndexOffset:2000,interactive:false}).addTo(m);}"
            "  });"
            " };"
            " window._gnssRegisterSelectable=function(layers,type,key){"
            "  var group={layers:layers,type:type,key:key||null};"
            "  window._gnssSelectableGroups.push(group);"
            "  layers.forEach(function(l){"
            "   try{if(l.options){l._gnssDefaultStyle={color:l.options.color,fillColor:l.options.fillColor,weight:l.options.weight,opacity:l.options.opacity,fillOpacity:l.options.fillOpacity};}}catch(e){}"
            "   if(l.on){l.on('click',function(ev){"
            "    if(ev&&ev.originalEvent){L.DomEvent.stop(ev.originalEvent);}"
            "    if(window._gnssMeasureActive){"
            "     var ll=null;try{ll=l.getLatLng?l.getLatLng():(l.getBounds?l.getBounds().getCenter():null);}catch(e){}"
            "     if(ll){window._gnssMeasureSnapKey=group.key||null;m.fire('click',{latlng:ll,originalEvent:ev.originalEvent||ev});}"
            "     return;"
            "    }"
            "    var ctrlHeld=(ev.originalEvent&&ev.originalEvent.ctrlKey)||!!window._gnssLiveMeasureMode;"
            "    if(ctrlHeld){"
            "     var idx=window._gnssMultiSelect.findIndex(function(s){return s.group===group;});"
            "     if(idx>=0){window._gnssRestoreStyle(window._gnssMultiSelect[idx].group);window._gnssMultiSelect.splice(idx,1);}"
            "     else{"
            "      if(window._gnssMultiSelect.length>=2){window._gnssRestoreStyle(window._gnssMultiSelect[0].group);window._gnssMultiSelect.shift();}"
            "      window._gnssHighlightMulti(group);window._gnssMultiSelect.push({group:group,key:group.key});"
            "     }"
            "     if(window._gnssMultiSelect.length<2){if(!window._gnssLiveMeasureMode)window._gnssRemoveMeasure();}"
            "     if(window._gnssMultiSelect.length===2){"
            "      if(window._gnssLiveMeasureMode){"
            "       if(!window._gnssLivePairs)window._gnssLivePairs=[];"
            "       window._gnssLivePairs.push({aKey:window._gnssMultiSelect[0].key,bKey:window._gnssMultiSelect[1].key,line:null,label:null});"
            "       if(window._gnssRefreshLivePairs)window._gnssRefreshLivePairs();"
            "       window._gnssMultiSelect.forEach(function(s){window._gnssRestoreStyle(s.group);});"
            "       window._gnssMultiSelect=[];"
            "      }else{"
            "       window._gnssLiveUpdateMeasure();"
            "      }"
            "     }"
            "    }else{"
            "     if(window._gnssMeasureLine){try{m.removeLayer(window._gnssMeasureLine);}catch(e){}window._gnssMeasureLine=null;}"
            "     if(window._gnssMeasureLabel){try{m.removeLayer(window._gnssMeasureLabel);}catch(e){}window._gnssMeasureLabel=null;}"
            "     window._gnssMultiSelect.forEach(function(s){window._gnssRestoreStyle(s.group);});"
            "     window._gnssMultiSelect=[];"
            "     window._gnssSelectGroup(group);"
            "    }"
            "   });}"
            "  });"
            "  return group;"
            " };"
            " function onDeleteKey(ev){"
            "  if(ev.key!=='Delete'&&ev.key!=='Backspace')return;"
            "  var group=window._gnssSelectedGroup;if(!group)return;"
            "  ev.preventDefault();"
            "  if(group.key){window._gnssDeletedRuntime[group.key]=true;}"
            "  group.layers.forEach(function(l){try{m.removeLayer(l);}catch(e){}});"
            "  window._gnssSelectableGroups=window._gnssSelectableGroups.filter(function(g){return g!==group;});"
            "  window._gnssSelectedGroup=null;"
            "  if(window._gnssRemoveMeasure)window._gnssRemoveMeasure();"
            " }"
            " document.addEventListener('keydown',onDeleteKey);"
            " if(m.getContainer()){m.getContainer().addEventListener('keydown',onDeleteKey);}"
            "}",
            "if(window.gnssEnsureSelectableTools)window.gnssEnsureSelectableTools(m);",
            "if (!window._gnssPopupCloseBound) {"
            " m.on('popupopen', function(e) {"
            "  var popup = e.popup;"
            "  var el = popup.getElement(); if (!el) return;"
            "  el.onclick = function(ev) {"
            "   var t = ev.target;"
            "   if (t.classList.contains('gnss-popup-close')) { m.closePopup(popup); return; }"
            "   if (t.classList.contains('gnss-add-flag')) {"
            "    var le=el.querySelector('.gnss-flag-lat'), ne=el.querySelector('.gnss-flag-lon'), ae=el.querySelector('.gnss-flag-alt');"
            "    var lat=parseFloat(le&&le.value), lon=parseFloat(ne&&ne.value);"
            "    if(isNaN(lat)||isNaN(lon)) return;"
            "    addUserFlag(lat, lon, (ae&&ae.value)||'-'); m.closePopup(popup); return;"
            "   }"
            "   if (t.classList.contains('gnss-cancel-flag')) { m.closePopup(popup); return; }"
            "  };"
            "  if (!popup._gnssDragSetup) {"
            "   popup._gnssDragSetup = true;"
            "   var _orig = popup._updatePosition.bind(popup);"
            "   popup._updatePosition = function() {"
            "    _orig();"
            "    var c = popup._container;"
            "    if (c && popup._gnssDragOff) {"
            "     var p = L.DomUtil.getPosition(c);"
            "     L.DomUtil.setPosition(c, {x: p.x + popup._gnssDragOff.x, y: p.y + popup._gnssDragOff.y});"
            "    }"
            "   };"
            "  }"
            "  popup._gnssDragOff = {x:0, y:0};"
            "  el.onmousedown = function(ev) {"
            "   var t = ev.target, isTitleBar = false;"
            "   while (t && t !== el) {"
            "    if (t.classList && t.classList.contains('gnss-popup-title')) { isTitleBar = true; break; }"
            "    t = t.parentNode;"
            "   }"
            "   if (!isTitleBar) return;"
            "   ev.preventDefault();"
            "   var sx=ev.clientX, sy=ev.clientY, ox=popup._gnssDragOff.x, oy=popup._gnssDragOff.y;"
            "   el.style.cursor='grabbing';"
            "   function mv(ev) { popup._gnssDragOff.x=ox+(ev.clientX-sx); popup._gnssDragOff.y=oy+(ev.clientY-sy); popup._updatePosition(); }"
            "   function up() { el.style.cursor=''; document.removeEventListener('mousemove',mv); document.removeEventListener('mouseup',up); }"
            "   document.addEventListener('mousemove',mv); document.addEventListener('mouseup',up);"
            "  };"
            " });"
            " window._gnssPopupCloseBound = true;"
            "}",
            "function card(title, lat, lon, alt, heading, sats, mode, actions) {"
            " var togBtn='<span onclick=\"window._gnssHeadingToggleReq=true;\" style=\"cursor:pointer;font-size:9pt;margin-right:6px;color:#a78bfa;user-select:none;\">&#x21FF;</span> ';"
            " var html = \"<div class='gnss-popup-card'><div class='gnss-popup-title'>\" + togBtn + \"<span>\" + esc(title) + \"</span><span class='gnss-popup-close'>×</span></div>\";"
            " html += \"<div class='gnss-popup-body'>\";"
            " html += \"<div class='gnss-popup-row'><span class='gnss-popup-key'>Latitude</span><span class='gnss-popup-val'>\" + fmtCoord(lat, 'lat') + \"</span></div>\";"
            " html += \"<div class='gnss-popup-row'><span class='gnss-popup-key'>Longitude</span><span class='gnss-popup-val'>\" + fmtCoord(lon, 'lon') + \"</span></div>\";"
            " if (alt !== null) html += \"<div class='gnss-popup-row'><span class='gnss-popup-key'>Alt</span><span class='gnss-popup-val'>\" + alt + \"</span></div>\";"
            " if (heading !== null) html += \"<div class='gnss-popup-row'><span class='gnss-popup-key'>Heading</span><span class='gnss-popup-val'>\" + heading + \"</span></div>\";"
            " if (sats !== null || mode !== null) html += \"<div class='gnss-popup-row'><span class='gnss-popup-key'>\" + (sats || '') + \"</span><span class='gnss-popup-val'>\" + (mode || '') + \"</span></div>\";"
            " if (actions) html += \"<div class='gnss-popup-foot'><span class='gnss-popup-btn'>Edit</span><span class='gnss-popup-btn'>Set as Base</span></div>\";"
            " return html + \"</div></div>\";"
            " }",
            "function flagForm(lat, lon) {"
            " return \"<div class='gnss-flag-form-card'><div class='gnss-popup-title'><span>FLAG EKLE</span><span class='gnss-popup-close'>×</span></div>\" +"
            " \"<div class='gnss-flag-form-body'>\" +"
            " \"<label>Latitude<input class='gnss-flag-lat' value='\" + lat.toFixed(8) + \"'><small>\" + fmtCoord(lat, 'lat') + \"</small></label>\" +"
            " \"<label>Longitude<input class='gnss-flag-lon' value='\" + lon.toFixed(8) + \"'><small>\" + fmtCoord(lon, 'lon') + \"</small></label>\" +"
            " \"<label>Alt<input class='gnss-flag-alt' value='0.0m'></label>\" +"
            " \"<div class='gnss-flag-actions'><span class='gnss-popup-btn gnss-cancel-flag'>Cancel</span><span class='gnss-popup-btn gnss-add-flag'>Add Flag</span></div>\" +"
            " \"</div></div>\";"
            "}",
            "function addUserFlag(lat, lon, alt) {"
            " if (!window._gnssUserFlags) window._gnssUserFlags = [];"
            " if (!window._gnssUserFlagData) window._gnssUserFlagData = [];"
            " var idx = window._gnssUserFlagData.length + 1;"
            " var title = 'FLAG-' + String(idx).padStart(2, '0');"
            " var marker = L.marker([lat, lon], {icon:flagIcon(), zIndexOffset:1200})"
            "  .bindPopup(card(title, lat, lon, String(alt||'-'), null, null, null, true),"
            "   {closeButton:true, autoClose:false, closeOnClick:false, autoPan:false, className:'gnss-command-popup'}).addTo(m);"
            " marker.off('click',marker._openPopup,marker);"
            " marker.on('dblclick',function(e){L.DomEvent.stop(e.originalEvent);marker.openPopup();});"
            " var lbl = L.marker([lat, lon], {icon:labelIcon(title + '<br>(USER FLAG)', 'gnss-map-label'), zIndexOffset:1199}).addTo(m);"
            " window._gnssUserFlags.push(marker); window._gnssUserFlags.push(lbl);"
            " if(window._gnssRegisterSelectable){window._gnssRegisterSelectable([marker,lbl],'flag','flag:'+title);}"
            " window._gnssUserFlagData.push({name:title,lat:lat,lon:lon,alt:String(alt||'-')});"
            "}",
            "if (!window._gnssContextDisabled) {"
            " m.on('contextmenu', function(e) { if(e.originalEvent){L.DomEvent.preventDefault(e.originalEvent);} });"
            " window._gnssContextDisabled = true;"
            "}",
        ]

        if len(source_items) >= 2 and show_baseline:
            coords = [[v["lat"], v["lon"]] for _, v in source_items]
            lines.append(
                f"window._gnssMarkers.push(L.polyline({coords},"
                "{color:'#0ea5e9',weight:3,opacity:0.9}).addTo(m));"
            )
            import math as _math
            _ordered = source_items[:]
            if self._primary_source_id and self._primary_target_id:
                _id_rank = {self._primary_source_id: 0, self._primary_target_id: 1}
                _ordered = sorted(source_items, key=lambda x: _id_rank.get(x[0], 99))
            _lat1, _lon1 = _ordered[0][1]["lat"], _ordered[0][1]["lon"]
            _lat2, _lon2 = _ordered[-1][1]["lat"], _ordered[-1][1]["lon"]
            _dlon = _math.radians(_lon2 - _lon1)
            _lr1, _lr2 = _math.radians(_lat1), _math.radians(_lat2)
            _y = _math.sin(_dlon) * _math.cos(_lr2)
            _x = _math.cos(_lr1) * _math.sin(_lr2) - _math.sin(_lr1) * _math.cos(_lr2) * _math.cos(_dlon)
            _brng = round((_math.degrees(_math.atan2(_y, _x)) + 360) % 360, 1)
            _svg = (
                f'<svg width="28" height="28" viewBox="-14 -14 28 28"'
                f' xmlns="http://www.w3.org/2000/svg"'
                f' style="transform:rotate({_brng}deg);overflow:visible">'
                '<polygon points="0,-12 -6,6 0,3 6,6"'
                ' fill="#38bdf8" stroke="#0369a1" stroke-width="1.5" stroke-linejoin="round"/>'
                '</svg>'
            )
            lines.append(
                f"(function(){{"
                f"var _svg={_svg!r};"
                f"var _ai=L.divIcon({{className:'gnss-heading-arrow',html:_svg,iconSize:[28,28],iconAnchor:[14,14]}});"
                f"var _am=L.marker([{_lat2},{_lon2}],{{icon:_ai,zIndexOffset:1050,interactive:false}}).addTo(m);"
                "window._gnssMarkers.push(_am);"
                "})()"
            )

        lines.append(
            "if(window._gnssDeletedRuntime){"
            " Object.keys(window._gnssDeletedRuntime).forEach(function(k){"
            "  if(k.indexOf('gps:')==0){delete window._gnssDeletedRuntime[k];}"
            " });"
            "}"
        )

        for dev_id, src_info in source_items:
            name = src_info["name"]
            lat  = src_info["lat"]
            lon  = src_info["lon"]
            meta = device_meta.get(dev_id, {})
            item_key = "gps:" + str(dev_id)
            sats_str = f"{meta.get('sats', '-')} uydu" if meta.get('sats') else '-'
            fix_str  = meta.get('fix_type', '-')
            quality_str = str(meta.get('fix_quality', '-'))
            hdop_str = f"{meta.get('hdop', 0.0):.2f}" if meta.get('hdop') else '-'
            alt_str  = f"{meta.get('alt', 0.0):.2f} m" if meta.get('alt') else 'N/A'
            last_update = meta.get('last_update') or 0
            last_str = time.strftime('%H:%M:%S', time.localtime(last_update / 1000.0)) if last_update else '-'
            raw_str = (str(meta.get('raw', ''))
                       .replace('\r', '').replace('\n', '')
                       .replace('\\', '\\\\')
                       .replace("<", "&lt;").replace(">", "&gt;"))
            lat_str  = meta.get('lat_fmt') or f"{lat:.6f}°"
            lon_str  = meta.get('lon_fmt') or f"{lon:.6f}°"
            popup_html = (
                f"<div class='gnss-popup-card'>"
                f"<div class='gnss-popup-title'><span>{name}</span>"
                f"<span class='gnss-popup-close'>×</span></div>"
                f"<div class='gnss-popup-body'>"
                f"<div class='gnss-popup-row'><span class='gnss-popup-key'>Latitude</span>"
                f"<span class='gnss-popup-val'>{lat_str}</span></div>"
                f"<div class='gnss-popup-row'><span class='gnss-popup-key'>Longitude</span>"
                f"<span class='gnss-popup-val'>{lon_str}</span></div>"
                f"<div class='gnss-popup-row'><span class='gnss-popup-key'>Alt</span>"
                f"<span class='gnss-popup-val'>{alt_str}</span></div>"
                f"<div class='gnss-popup-row'><span class='gnss-popup-key'>Fix Quality</span>"
                f"<span class='gnss-popup-val'>{quality_str}</span></div>"
                f"<div class='gnss-popup-row'><span class='gnss-popup-key'>Fix</span>"
                f"<span class='gnss-popup-val'>{fix_str}</span></div>"
                f"<div class='gnss-popup-row'><span class='gnss-popup-key'>Satellites</span>"
                f"<span class='gnss-popup-val'>{sats_str}</span></div>"
                f"<div class='gnss-popup-row'><span class='gnss-popup-key'>HDOP</span>"
                f"<span class='gnss-popup-val'>{hdop_str}</span></div>"
                f"<div class='gnss-popup-row'><span class='gnss-popup-key'>Last</span>"
                f"<span class='gnss-popup-val'>{last_str}</span></div>"
                f"<div class='gnss-popup-row'><span class='gnss-popup-key'>Raw GPGGA</span>"
                f"<span class='gnss-popup-val' style='font-size:10px;word-break:break-all;text-align:left;'>{raw_str}</span></div>"
                f"</div></div>"
            ).replace("'", "\\'")
            safe_name = str(name).replace("'", "\\'")
            safe_key = item_key.replace("'", "\\'")
            lines.append(
                f"if(!window._gnssDeletedRuntime||!window._gnssDeletedRuntime['{safe_key}']){{"
                f" _activePmk['{safe_key}']=true;"
                f" if(window._gnssPmk['{safe_key}']){{"
                f"  window._gnssPmk['{safe_key}'].setLatLng([{lat},{lon}]);"
                f"  if(window._gnssPmk['{safe_key}'].getPopup()){{window._gnssPmk['{safe_key}'].getPopup().setContent('{popup_html}');}}"
                f" }}else{{"
                f"  var gpsMk=L.circleMarker([{lat},{lon}],"
                f"  {{radius:9,color:'#38bdf8',fillColor:'#0ea5e9',fillOpacity:0.95,weight:3}})"
                f"  .bindPopup('{popup_html}',"
                "  {closeButton:false,autoClose:false,closeOnClick:false,autoPan:false,className:'gnss-command-popup'}).addTo(m);"
                "  gpsMk.off('click',gpsMk._openPopup,gpsMk);"
                "  gpsMk.on('dblclick',function(e){L.DomEvent.stop(e.originalEvent);gpsMk.openPopup();});"
                f"  if(window._gnssRegisterSelectable){{window._gnssRegisterSelectable([gpsMk],'gps','{safe_key}');}}"
                f"  window._gnssPmk['{safe_key}']=gpsMk;"
                " }"
            )
            lines.append(
                f" var gpsLbl=L.marker([{lat},{lon}],"
                f" {{icon:labelIcon('{safe_name}','gnss-map-label',[-12,9]),zIndexOffset:800}}).addTo(m);"
                " window._gnssMarkers.push(gpsLbl);"
                "}"
            )

        if len(source_items) >= 2 and self._filtered_midpoint is not None:
            mid_lat, mid_lon = self._filtered_midpoint
            for _, v in source_items:
                lines.append(
                    f"window._gnssMarkers.push(L.polyline([[{v['lat']},{v['lon']}],[{mid_lat},{mid_lon}]],"
                    "{color:'#facc15',weight:1.5,opacity:0.55,dashArray:'6 6'}).addTo(m));"
                )

        if self._raw_midpoint is not None:
            lat, lon = self._raw_midpoint
            lines.append(
                "if(!window._gnssDeletedRuntime||!window._gnssDeletedRuntime['centroid:raw']){"
                " _activePmk['centroid:raw']=true;"
                " if(window._gnssPmk['centroid:raw']){"
                f"  window._gnssPmk['centroid:raw'].setLatLng([{lat},{lon}]);"
                f"  if(window._gnssPmk['centroid:raw'].getPopup()){{window._gnssPmk['centroid:raw'].getPopup().setContent(card('CP-01 RAW',{lat},{lon},null,null,null,null,false));}}"
                " }else{"
                f"  var rawMk=L.circleMarker([{lat},{lon}],"
                "  {radius:8,color:'#111827',fillColor:'#facc15',fillOpacity:0.75,weight:2})"
                f"  .bindPopup(card('CP-01 RAW',{lat},{lon},null,null,null,null,false),"
                "  {closeButton:false,autoClose:false,closeOnClick:false,autoPan:false,className:'gnss-command-popup'}).addTo(m);"
                "  rawMk.off('click',rawMk._openPopup,rawMk);"
                "  rawMk.on('dblclick',function(e){L.DomEvent.stop(e.originalEvent);rawMk.openPopup();});"
                "  if(window._gnssRegisterSelectable){window._gnssRegisterSelectable([rawMk],'centroid','centroid:raw');}"
                "  window._gnssPmk['centroid:raw']=rawMk;"
                " }"
                "}"
            )

        if self._filtered_midpoint is not None:
            lat, lon = self._filtered_midpoint
            lines.append(
                "if(!window._gnssDeletedRuntime||!window._gnssDeletedRuntime['centroid:filtered']){"
                " _activePmk['centroid:filtered']=true;"
                " if(window._gnssPmk['centroid:filtered']){"
                f"  window._gnssPmk['centroid:filtered'].setLatLng([{lat},{lon}]);"
                f"  if(window._gnssPmk['centroid:filtered'].getPopup()){{window._gnssPmk['centroid:filtered'].getPopup().setContent(card('CENTER POINT',{lat},{lon},null,null,null,'CP-01',false));}}"
                " }else{"
                f"  var centerMk=L.circleMarker([{lat},{lon}],"
                "  {radius:8,color:'#7f1d1d',fillColor:'#ef4444',fillOpacity:0.92,weight:3})"
                f"  .bindPopup(card('CENTER POINT',{lat},{lon},null,null,null,'CP-01',false),"
                "  {closeButton:false,autoClose:false,closeOnClick:false,autoPan:false,className:'gnss-command-popup'}).addTo(m);"
                "  centerMk.off('click',centerMk._openPopup,centerMk);"
                "  centerMk.on('dblclick',function(e){L.DomEvent.stop(e.originalEvent);centerMk.openPopup();});"
                "  if(window._gnssRegisterSelectable){window._gnssRegisterSelectable([centerMk],'centroid','centroid:filtered');}"
                "  window._gnssPmk['centroid:filtered']=centerMk;"
                " }"
            )
            lines.append(
                f" var centerLbl=L.marker([{lat},{lon}],"
                f" {{icon:labelIcon('CENTER POINT<br>(CP-01)','gnss-center-label',[50,-10]),zIndexOffset:950}}).addTo(m);"
                " window._gnssMarkers.push(centerLbl);"
                "}"
            )

        lines.append(
            "Object.keys(window._gnssPmk).forEach(function(k){"
            " if(!_activePmk[k]){try{m.removeLayer(window._gnssPmk[k]);}catch(e){}delete window._gnssPmk[k];}"
            "});"
        )

        # Centroid'i window'a kaydet (Orta Nokta aracı için)
        if self._filtered_midpoint is not None:
            clat2, clon2 = self._filtered_midpoint
            lines.append(f"window._gnssCentroid = [{clat2}, {clon2}];")
        else:
            lines.append("window._gnssCentroid = null;")

        # Katman toggle fonksiyonu (sadece ilk çalıştırmada tanımla)
        if self._offline_layers:
            layer_js = []
            for layer in self._offline_layers:
                layer_js.append({
                    "name": str(layer["name"]).replace("'", "\\'"),
                    "url": str(layer["url"]).replace("'", "\\'"),
                    "maxNativeZoom": int(layer.get("max_native_zoom", 11)),
                    "maxZoom": int(layer.get("max_zoom", 22)),
                })
            lines.append(
                "if (!window._gnssLayerDefined) {"
                f" window._gnssOfflineLayerDefs = {layer_js};"
                " window._gnssLayerDefined = true;"
                " window._gnssCurrentLayerIndex = 0;"
                " window._gnssToggleLayer = function() {"
                "  if (!window._gnssOfflineLayerDefs || window._gnssOfflineLayerDefs.length < 2) return;"
                "  window._gnssCurrentLayerIndex = (window._gnssCurrentLayerIndex + 1) % window._gnssOfflineLayerDefs.length;"
                "  var def = window._gnssOfflineLayerDefs[window._gnssCurrentLayerIndex];"
                "  m.eachLayer(function(l){ if(l._gnssOfflineManaged || (l._url && l._url.indexOf('/tiles/')>-1)) { try{m.removeLayer(l);}catch(e){} } });"
                "  var nl = L.tileLayer(def.url, {attribution:def.name, maxZoom:def.maxZoom, maxNativeZoom:def.maxNativeZoom});"
                "  nl._gnssOfflineManaged = true;"
                "  nl.addTo(m);"
                "  nl.bringToBack();"
                " };"
                "}"
            )
        else:
            lines.append(
                "if (!window._gnssLayerDefined) {"
                " window._gnssLayerDefined = true;"
                " window._gnssOverlaysVisible = true;"
                " window._gnssSeaMarksLayer = null;"
                " window._gnssMarineProfileLayer = null;"
                " window._gnssDepthLayer = null;"
                " m.eachLayer(function(l){"
                "  if(l._url && l._url.indexOf('openseamap.org/seamark')>-1) window._gnssSeaMarksLayer = l;"
                "  if(l.wmsParams && String(l.wmsParams.layers || '').indexOf('deepshade')>-1) window._gnssMarineProfileLayer = l;"
                "  if(l.wmsParams && String(l.wmsParams.layers || '').indexOf('contour')>-1) window._gnssDepthLayer = l;"
                " });"
                " window._gnssEnsureSeaLayers = function(){"
                "  if(!window._gnssSeaMarksLayer){window._gnssSeaMarksLayer=L.tileLayer('https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png',{attribution:'OpenSeaMap',maxZoom:25,maxNativeZoom:18});}"
                "  if(!window._gnssMarineProfileLayer){window._gnssMarineProfileLayer=L.tileLayer.wms('https://geoserver.openseamap.org/geoserver/wms',{layers:'gebco2021:gebco_2021',format:'image/png',transparent:true,attribution:'OpenSeaMap / GEBCO 2021'});}"
                "  if(!window._gnssDepthLayer){window._gnssDepthLayer=L.tileLayer.wms('https://geoserver.openseamap.org/geoserver/wms',{layers:'gebco2021:gebco2021_contour',format:'image/png',transparent:true,attribution:'OpenSeaMap / GEBCO 2021 contours'});}"
                " };"
                " window._gnssToggleLayer = function() {"
                "  window._gnssEnsureSeaLayers();"
                "  if (window._gnssOverlaysVisible) {"
                "   if(window._gnssSeaMarksLayer && m.hasLayer(window._gnssSeaMarksLayer)) m.removeLayer(window._gnssSeaMarksLayer);"
                "   if(window._gnssMarineProfileLayer && m.hasLayer(window._gnssMarineProfileLayer)) m.removeLayer(window._gnssMarineProfileLayer);"
                "   if(window._gnssDepthLayer && m.hasLayer(window._gnssDepthLayer)) m.removeLayer(window._gnssDepthLayer);"
                "   window._gnssOverlaysVisible = false;"
                "  } else {"
                "   if(!m.hasLayer(window._gnssMarineProfileLayer)) window._gnssMarineProfileLayer.addTo(m);"
                "   if(!m.hasLayer(window._gnssSeaMarksLayer)) window._gnssSeaMarksLayer.addTo(m);"
                "   if(!m.hasLayer(window._gnssDepthLayer)) window._gnssDepthLayer.addTo(m);"
                "   window._gnssOverlaysVisible = true;"
                "  }"
                " };"
                "}"
            )

        # Kilit açıksa haritayı otomatik ortala (zoom değişmez)
        if pan:
            clat, clon = self._center
            lines.append(f"m.setView([{clat},{clon}], Math.max(m.getZoom(), {self.zoom}), {{animate:false}});")

        lines.append(
            "if(window._gnssLiveUpdateMeasure&&window._gnssMultiSelect&&window._gnssMultiSelect.length===2){"
            " window._gnssLiveUpdateMeasure();"
            "}"
            "if(window._gnssRefreshLivePairs&&window._gnssLivePairs&&window._gnssLivePairs.length>0){"
            " window._gnssRefreshLivePairs();"
            "}"
        )
        lines.append("} catch(e) { console.error('[GNSS]', e); } })();")
        return "\n".join(lines)

    @staticmethod
    def _add_marker(folium, folium_map, lat: float, lon: float, style: MarkerStyle, name: str) -> None:
        popup = f"{style.label}: {name}<br>Lat: {lat:.8f}<br>Lon: {lon:.8f}"
        folium.Marker(
            location=(lat, lon),
            popup=popup,
            tooltip=f"{style.label} - {name}",
            icon=folium.Icon(color=style.color, icon=style.icon),
        ).add_to(folium_map)
