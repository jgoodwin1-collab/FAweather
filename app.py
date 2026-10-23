import re
import html
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import folium
import requests
import streamlit as st
from streamlit_folium import st_folium
from branca.element import MacroElement, Template

st.set_page_config(page_title='FAweather | Kansas Weather Alerts', page_icon='🌪️', layout='wide')

# Replace with an email address you control before publishing.
USER_AGENT = 'FAweather/2.0 (contact: replace-with-your-email@example.com)'
HEADERS = {'User-Agent': USER_AGENT, 'Accept': 'application/geo+json'}
# TEMPORARY TEST SWITCH: set False to restore Kansas-only coverage.
TEST_THREE_STATES = True
ACTIVE_STATES = ('KS', 'AZ', 'NM') if TEST_THREE_STATES else ('KS',)
STATE_NAMES = {'KS': 'Kansas', 'AZ': 'Arizona', 'NM': 'New Mexico'}
REGION_LABEL = 'Kansas, Arizona & New Mexico' if TEST_THREE_STATES else 'Kansas'
MAP_BOUNDS = ([[31.2, -109.1], [40.1, -94.5]] if TEST_THREE_STATES
              else [[36.99, -102.05], [40.01, -94.59]])

CENTRAL = ZoneInfo('America/Chicago')


def central_time(value, include_date=True):
    """Format UTC/offset timestamps for Kansas visitors, with automatic CST/CDT."""
    if value is None or value == '':
        return 'Not specified'
    try:
        if isinstance(value, (int, float)):
            stamp = float(value)
            # ArcGIS dates are commonly Unix milliseconds.
            if abs(stamp) > 1e11:
                stamp /= 1000
            dt = datetime.fromtimestamp(stamp, tz=timezone.utc)
        elif isinstance(value, datetime):
            dt = value
        else:
            raw = str(value).strip()
            if raw.isdigit() and len(raw) in (10, 13):
                return central_time(int(raw), include_date)
            dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            # NOAA's timezone-less timestamps are treated as UTC.
            dt = dt.replace(tzinfo=timezone.utc)
        fmt = '%b %d, %Y at %I:%M %p %Z' if include_date else '%I:%M %p %Z'
        return dt.astimezone(CENTRAL).strftime(fmt).replace(' at 0', ' at ').lstrip('0')
    except (ValueError, TypeError, OverflowError, OSError):
        return str(value) + ' (time format unrecognized)'


st.markdown('''<style>
.stApp {background: #0b1423; color: #e9f1fb;}
[data-testid="stSidebar"] {background: #121f32;}
h1,h2,h3 {color: #f2f7ff !important;}
[data-testid="stMetric"] {background:#14243a;border:1px solid #30435c;border-radius:12px;padding:12px;}
[data-testid="stMetricLabel"] {color:#c2d4e8;}
[data-testid="stMetricValue"] {color:#ffffff;}
.weather-note {background:#14243a;border-left:4px solid #57a9f8;padding:12px 16px;border-radius:7px;margin:10px 0 18px;}
</style>''', unsafe_allow_html=True)


def api_json(url, params=None):
    response = requests.get(url, params=params, headers=HEADERS, timeout=25)
    response.raise_for_status()
    return response.json()


@st.cache_data(ttl=86400, show_spinner=False)
def lookup_zip(code):
    response = requests.get(f'https://api.zippopotam.us/us/{code}', timeout=15)
    response.raise_for_status()
    places = response.json().get('places') or []
    if not places:
        raise ValueError('ZIP code not found.')
    place = places[0]
    if place.get('state abbreviation', '').upper() not in ACTIVE_STATES:
        raise ValueError('Please enter a ZIP code in ' + REGION_LABEL + '.')
    return {'city': place['place name'], 'state': place.get('state', ''), 'zip': code,
            'lat': float(place['latitude']), 'lon': float(place['longitude'])}


@st.cache_data(ttl=3600, show_spinner=False)
def nws_point(lat, lon):
    return api_json(f'https://api.weather.gov/points/{lat:.4f},{lon:.4f}')['properties']


@st.cache_data(ttl=600, show_spinner=False)
def nws_forecast(url):
    return api_json(url)['properties']['periods']


@st.cache_data(ttl=120, show_spinner=False)
def active_alerts(area=None, point=None):
    params = {'area': area} if area else {'point': point}
    data = api_json('https://api.weather.gov/alerts/active', params=params)
    return data.get('features', []), datetime.now(timezone.utc).isoformat()


@st.cache_data(ttl=300, show_spinner=False)
def radar_history():
    """Get observed radar frames from RainViewer; never interpret as forecast."""
    response = requests.get('https://api.rainviewer.com/public/weather-maps.json', timeout=15)
    response.raise_for_status()
    data = response.json()
    frames = (data.get('radar') or {}).get('past') or []
    host = data.get('host', '')
    if not host.startswith('https://') or not host.endswith('.rainviewer.com'):
        raise ValueError('Unexpected radar host from RainViewer')
    result = []
    for frame in frames:
        path = frame.get('path', '')
        stamp = frame.get('time')
        if path.startswith('/v2/radar/') and isinstance(stamp, (int, float)):
            result.append({'url': host + path + '/256/{z}/{x}/{y}/2/1_1.png',
                           'label': central_time(stamp, include_date=False),
                           'time': stamp})
    if not result:
        raise ValueError('No radar frames currently available')
    return result


class RadarPlayback(MacroElement):
    """Leaflet playback control lives inside the map, so Streamlit reruns are unnecessary."""
    _template = Template(r"""
    {% macro script(this, kwargs) %}
    (function () {
      var map = {{this._parent.get_name()}};
      var frames = {{this.frames_json|safe}};
      var current = frames.length - 1;
      // Animate the Folium-managed overlay already registered in LayerControl.
      // Do not call bringToBack(): that puts transparent radar UNDER OpenStreetMap.
      var radar = {{this.radar_layer_name}};
      var control = L.control({position:'bottomleft'});
      control.onAdd = function () {
        var wrap = L.DomUtil.create('div');
        wrap.style.cssText = 'background:#101e31;color:white;padding:10px 12px;border:1px solid #7b8ca4;border-radius:8px;min-width:235px;box-shadow:0 2px 8px #0008;font:13px sans-serif';
        var row = L.DomUtil.create('div', '', wrap);
        row.style.cssText = 'display:flex;align-items:center;gap:8px;margin-bottom:6px';
        var button = L.DomUtil.create('button', '', row);
        button.type = 'button'; button.textContent = '▶ Play';
        button.style.cssText = 'padding:5px 10px;cursor:pointer;border-radius:5px;border:1px solid #8fa3bb;background:#223e60;color:white';
        var label = L.DomUtil.create('span', '', row);
        var slider = L.DomUtil.create('input', '', wrap);
        slider.type = 'range'; slider.min = 0; slider.max = frames.length - 1;
        slider.step = 1; slider.value = current; slider.style.width = '100%';
        var note = L.DomUtil.create('div', '', wrap);
        note.textContent = 'Past observed radar • not a forecast';
        note.style.cssText = 'font-size:11px;color:#c5d5e9;margin-top:4px';
        var timer = null;
        function show(i) {
          current = i;
          radar.setUrl(frames[i].url);
          slider.value = i;
          label.textContent = frames[i].label;
        }
        function stop() {
          if (timer !== null) {clearInterval(timer);timer = null;}
          button.textContent = '▶ Play';
        }
        button.onclick = function () {
          if (timer !== null) {stop();return;}
          button.textContent = '❚❚ Pause';
          timer = setInterval(function () {show((current + 1) % frames.length);}, 900);
        };
        slider.oninput = function () {stop();show(Number(slider.value));};
        map.on('unload', stop);
        L.DomEvent.disableClickPropagation(wrap);
        L.DomEvent.disableScrollPropagation(wrap);
        show(current);
        return wrap;
      };
      control.addTo(map);
    })();
    {% endmacro %}
    """)

    def __init__(self, frames, radar_layer):
        super().__init__()
        self._name = 'RadarPlayback'
        self.frames_json = json.dumps(frames)
        self.radar_layer_name = radar_layer.get_name()


def category(feature):
    props = feature.get('properties') or {}
    event = (props.get('event') or '').lower()
    if 'tornado warning' in event:
        return 'Tornado warning', '#ff3b4d'
    if 'severe thunderstorm warning' in event:
        return 'Severe thunderstorm warning', '#ffad3d'
    if 'flash flood warning' in event:
        return 'Flash flood warning', '#ba7cff'
    if 'flood warning' in event:
        return 'Flood warning', '#7f91ff'
    if 'tornado watch' in event:
        return 'Tornado watch', '#ff7791'
    if 'severe thunderstorm watch' in event:
        return 'Severe thunderstorm watch', '#f2d35c'
    return 'Other NWS alert', '#4db9d5'


def elevated_tag(feature):
    p = feature.get('properties') or {}
    params = p.get('parameters') or {}
    text = ' '.join(str(p.get(k) or '') for k in ('headline', 'description', 'event')).upper()
    if 'TORNADO EMERGENCY' in text:
        return 'TORNADO EMERGENCY'
    if 'PARTICULARLY DANGEROUS SITUATION' in text or 'PDS' in str(params.get('PDS', '')).upper():
        return 'PDS — Particularly Dangerous Situation'
    damage = ' '.join(str(x) for x in params.get('damageThreat', [])) if isinstance(params.get('damageThreat'), list) else str(params.get('damageThreat') or '')
    if damage.lower() == 'destructive':
        return 'DESTRUCTIVE THUNDERSTORM'
    return None


def show_alert(feature):
    p = feature.get('properties') or {}
    label, _ = category(feature)
    tag = elevated_tag(feature)
    title = p.get('event') or label
    st.markdown(f'**{title}**' + (f' — **{tag}**' if tag else ''))
    st.caption(f"{p.get('areaDesc') or 'Area not specified'} | Ends: {central_time(p.get('ends') or p.get('expires'))}")
    if p.get('headline'):
        st.write(p['headline'])
    with st.expander('Full alert text and safety instructions'):
        if p.get('description'):
            st.text(p['description'])
        if p.get('instruction'):
            st.warning(p['instruction'])
        if p.get('id'):
            st.markdown(f"[Official NWS alert]({p['id']})")
    st.divider()


def make_warning_map(alerts, loc, radar_frames=None):
    m = folium.Map(location=[36.0, -102.0] if TEST_THREE_STATES else [38.5, -98], zoom_start=6 if TEST_THREE_STATES else 7, tiles='OpenStreetMap', control_scale=True)
    # Keep radar as a genuine Folium overlay: visible above OpenStreetMap,
    # below NWS vector polygons, and available in the map's layer selector.
    if radar_frames:
        radar_layer = folium.TileLayer(
            tiles=radar_frames[-1]['url'],
            name='Animated Radar (RainViewer)',
            attr='Radar imagery: RainViewer',
            overlay=True,
            control=True,
            show=True,
            opacity=0.70,
            max_native_zoom=7,
            max_zoom=18,
        ).add_to(m)
        m.add_child(RadarPlayback(radar_frames, radar_layer))
    drawn = 0
    for feature in alerts:
        geom = feature.get('geometry')
        if not geom or geom.get('type') not in ('Polygon', 'MultiPolygon'):
            continue
        p = feature.get('properties') or {}
        label, color = category(feature)
        title = html.escape(p.get('event') or label)
        area = html.escape(p.get('areaDesc') or 'Area not specified')
        ends = html.escape(central_time(p.get('ends') or p.get('expires')))
        tag = elevated_tag(feature)
        special = f'<p><strong>{html.escape(tag)}</strong></p>' if tag else ''
        popup_html = f'<div style="width:300px"><h4>{title}</h4>{special}<p>{area}</p><p>Ends: {ends}</p></div>'
        folium.GeoJson(
            geom,
            name=label,
            style_function=lambda _, c=color: {'color': c, 'weight': 3, 'fillColor': c, 'fillOpacity': 0.30},
            highlight_function=lambda _: {'weight': 5, 'fillOpacity': 0.48},
            tooltip=f"{p.get('event') or label} — {p.get('areaDesc') or 'Region'}",
            popup=folium.Popup(popup_html, max_width=340),
        ).add_to(m)
        drawn += 1
    folium.Marker([loc['lat'], loc['lon']], tooltip=f"{loc['city']} ({loc['zip']})",
                  icon=folium.Icon(color='blue', icon='home')).add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    m.fit_bounds(MAP_BOUNDS)
    return m, drawn


st.title('🌪️ FAweather | Severe Weather Center')
if TEST_THREE_STATES:
    st.warning('TEMPORARY TEST MODE: Showing Kansas, Arizona and New Mexico. All displayed times remain Central Time (CST/CDT), including Arizona and New Mexico reports.')
st.caption('Official NWS alerts • Animated radar + warning polygons on one map • Local forecasts')
st.markdown('<div class="weather-note">⚠️ This site is supplemental weather information, not an emergency notification system. Keep Wireless Emergency Alerts and NOAA Weather Radio enabled. Always follow official NWS instructions.</div>', unsafe_allow_html=True)

with st.sidebar:
    st.header('Your location')
    zipcode = st.text_input('ZIP code', '67202', max_chars=5).strip()
    refresh = st.button('Refresh all data', type='primary', width='stretch')
    st.caption('Alerts automatically refresh about every 2 minutes while this page is open. Forecasts refresh every 10 minutes.')
    st.caption('Coverage: ' + REGION_LABEL)
    st.markdown('[NWS radar viewer](https://radar.weather.gov/)')

if refresh:
    st.cache_data.clear()
if len(zipcode) != 5 or not zipcode.isdigit():
    st.info('Enter a valid five-digit ZIP code.')
    st.stop()

try:
    loc = lookup_zip(zipcode)
    point = nws_point(loc['lat'], loc['lon'])
except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
    st.error(f'Could not load your Kansas location: {exc}')
    st.stop()

st.subheader(f"📍 {loc['city']}, {loc['state']} {loc['zip']}")
st.caption(f"NWS forecast office: {point.get('gridId', 'Unknown')}")


@st.fragment(run_every='2min')
def live_warnings():
    st.subheader('Live weather warnings — ' + REGION_LABEL)
    try:
        statewide = []
        fetched_at = None
        for state_code in ACTIVE_STATES:
            state_alerts, state_fetched = active_alerts(area=state_code)
            statewide.extend(state_alerts)
            fetched_at = state_fetched
        # Alerts may span state boundaries; avoid double-counting shared NWS alert IDs.
        statewide = list({a.get('id') or (a.get('properties') or {}).get('id') or str(i): a
                          for i, a in enumerate(statewide)}.values())
        local, _ = active_alerts(point=f"{loc['lat']:.4f},{loc['lon']:.4f}")
    except requests.RequestException as exc:
        st.error(f'NWS alerts could not be refreshed: {exc}. Do not assume there are no active warnings.')
        st.markdown('[Check the official NWS alerts](https://www.weather.gov/)')
        return

    fetched = central_time(fetched_at)
    st.caption(f'Last successful NWS alert fetch: {fetched} • Automatic check approximately every 2 minutes while open')
    counts = {key: sum(category(a)[0] == key for a in statewide) for key in
              ('Tornado warning', 'Severe thunderstorm warning', 'Flash flood warning')}
    cols = st.columns(4)
    cols[0].metric('Active regional alerts', len(statewide))
    cols[1].metric('Tornado warnings', counts['Tornado warning'])
    cols[2].metric('Severe T-storm warnings', counts['Severe thunderstorm warning'])
    cols[3].metric('Flash flood warnings', counts['Flash flood warning'])

    priority = [a for a in statewide if elevated_tag(a)]
    if priority:
        st.error(f'⚠️ {len(priority)} high-priority alert(s) detected. Review the alert details immediately.')
        for a in priority:
            st.write(f"**{elevated_tag(a)}:** {(a.get('properties') or {}).get('areaDesc') or 'See alert details'}")

    st.markdown('**Radar + NWS warning map** — radar precipitation beneath clickable official warning polygons.')
    frames = None
    try:
        frames = radar_history()
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        st.warning(f'Radar imagery is temporarily unavailable ({exc}). NWS warnings are still displayed.')
    if frames:
        first = central_time(frames[0]['time'])
        last = central_time(frames[-1]['time'])
        st.caption(f'Radar history: {first} to {last} • {len(frames)} frames • Use ▶ Play, ❚❚ Pause or the timeline **inside the map**. Frames can be delayed.')
    m, drawn = make_warning_map(statewide, loc, frames)
    st_folium(m, width='stretch', height=600, returned_objects=[], key='ks_warning_map')
    missing = len(statewide) - drawn
    if missing:
        st.info(f'{missing} active alert(s) have no usable polygon geometry and are listed in text below. Map shapes alone do not show every alert.')
    st.caption('Radar: RainViewer public API • Warning polygons: NWS. Radar imagery is observational, may be delayed, and is not a forecast.')
    st.caption('Map colors: 🔴 Tornado warning • 🟠 Severe thunderstorm warning • 🟣 Flash flood warning • other alerts shown in distinct colors.')

    st.subheader('Warnings affecting your selected location')
    if local:
        for a in local:
            show_alert(a)
    else:
        st.info('NWS returned no active alerts for this ZIP-code point. Alerts elsewhere in the ZIP code may differ.')

    with st.expander(f'All regional alerts ({len(statewide)})', expanded=False):
        if not statewide:
            st.info('NWS returned no active regional alerts at the last successful check.')
        for a in sorted(statewide, key=lambda f: 0 if 'warning' in ((f.get('properties') or {}).get('event') or '').lower() else 1):
            show_alert(a)


live_warnings()


# Independent NOAA Local Storm Reports map (24-hour service layer).
LSR_URL = ('https://mapservices.weather.noaa.gov/vector/rest/services/'
           'obs/nws_local_storm_reports/MapServer/0/query')


@st.cache_data(ttl=1800, show_spinner=False)
def regional_storm_reports():
    """Read reports for configured states from NOAA rolling 24-hour layer."""
    reports = []
    offset = 0
    page_size = 1000  # Below NOAA's advertised maxRecordCount of 2000.
    while True:
        params = {
            'where': ("state IN ('KS','AZ','NM')" if TEST_THREE_STATES else "state = 'KS'"), 'outFields': '*', 'returnGeometry': 'true',
            'outSR': 4326, 'f': 'json', 'resultOffset': offset,
            'resultRecordCount': page_size, 'orderByFields': 'objectid ASC',
        }
        response = requests.get(LSR_URL, params=params, timeout=30,
                                headers={'User-Agent': USER_AGENT})
        response.raise_for_status()
        data = response.json()
        if 'error' in data:
            raise ValueError(str(data['error'].get('message', 'NOAA query failed')))
        batch = data.get('features', [])
        reports.extend(batch)
        if len(batch) < page_size and not data.get('exceededTransferLimit'):
            break
        if not batch:
            break
        offset += len(batch)
        if offset > 50000:
            raise ValueError('Unexpectedly large NOAA result set; report list incomplete')
    return reports, datetime.now(timezone.utc).isoformat()


def hail_size_label(magnitude, units):
    """Display a reported hail diameter in inches, or None if unavailable.

    NOAA's LSR layer uses numeric magnitude and a separate units field.
    Avoid presenting a non-inch value as inches without conversion.
    """
    if magnitude is None or str(magnitude).strip() == '':
        return None
    raw = str(magnitude).strip().replace(',', '')
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)\s*(?:in(?:ches)?|\")?', raw, flags=re.I)
    if not match:
        return None
    value = float(match.group(1))
    if not 0 < value < 30:
        return None
    unit = str(units or '').strip().lower()
    if unit in ('mm', 'millimeter', 'millimeters'):
        value /= 25.4
    elif unit in ('cm', 'centimeter', 'centimeters'):
        value /= 2.54
    elif unit not in ('', 'in', 'inch', 'inches', '\"'):
        return None
    return f'{value:g}\"'


def lsr_style(event):
    """Choose short readable labels and weather-inspired marker classes."""
    event = (event or '').lower()
    if 'tornado' in event or 'landspout' in event:
        return 'TORNADO', 'tornado', 'Tornado'
    if 'hail' in event:
        return 'HAIL', 'hail', 'Hail'
    if 'fog' in event:
        return 'FOG', 'fog', 'Fog'
    if 'thunder' in event or 'lightning' in event:
        return 'STORM', 'storm', 'Thunder / Lightning'
    if 'wind' in event or 'wnd' in event or 'downburst' in event:
        return 'WIND', 'wind', 'Wind'
    if 'flood' in event:
        return 'FLOOD', 'flood', 'Flood'
    if 'rain' in event:
        return 'RAIN', 'rain', 'Rain'
    if 'blizzard' in event:
        return 'BLIZZARD', 'snow', 'Winter'
    if 'snow' in event:
        return 'SNOW', 'snow', 'Winter'
    if 'ice' in event or 'sleet' in event or 'freezing' in event:
        return 'ICE', 'ice', 'Winter'
    if 'dust' in event:
        return 'DUST', 'fog', 'Dust'
    return 'REPORT', 'other', 'Other'


# Styles are scoped to the LSR map's marker icons; the radar map is unchanged.
LSR_MARKER_CSS = r"""
<style>
.lsr-label {position:relative;display:inline-block;white-space:nowrap;transform:translate(-50%,-50%);
 font-family:Impact,'Arial Black',sans-serif;font-size:16px;font-weight:900;letter-spacing:.6px;
 line-height:1.15;padding:4px 7px;border-radius:6px;pointer-events:auto;
 filter:drop-shadow(0 2px 2px rgba(0,0,0,.9));}
.lsr-label::after {content:'';position:absolute;left:50%;bottom:-5px;transform:translateX(-50%);
 width:6px;height:6px;border-radius:50%;background:#fff;border:1px solid #152238;}
.lsr-hail {color: #ffffff !important;
    font-family: Arial, sans-serif !important;
    font-weight: 900 !important;
    font-style: normal !important;
    font-size: 18px !important;
    letter-spacing: 0 !important;
    text-shadow: none !important;
    filter: none !important;
    -webkit-text-stroke: 0 !important;
    opacity: 1 !important;
    white-space: nowrap !important;}
.lsr-fog {color:#d9dce0;text-shadow:0 0 4px #fff,0 0 8px #bfc4ce,0 0 13px #999;
 background:rgba(77,87,99,.78);border:1px solid #a6b0b9;filter:blur(.35px) drop-shadow(0 2px 2px #111);}
.lsr-rain,.lsr-flood {color:#d3faff;text-shadow:0 1px #115a93,0 2px #07385b;
 background:linear-gradient(145deg,#42c6ed,#0874b6 65%,#045177);border:1px solid #8fe5ff;}
.lsr-tornado {color:#fff2f2;text-shadow:1px 2px #7c1727,2px 3px #450813;
 background:linear-gradient(120deg,#f85165,#9b172c);border:1px solid #ffadb5;transform:translate(-50%,-50%) skew(-6deg);}
.lsr-wind {color:#fff2d4;text-shadow:2px 1px #95501b;
 background:linear-gradient(110deg,#f7bd64,#bf681f);border:1px solid #ffdc9a;transform:translate(-50%,-50%) skew(-8deg);}
.lsr-snow,.lsr-ice {color:#f5fcff;text-shadow:1px 2px #638bb6;
 background:linear-gradient(135deg,#d2edff,#809fc9);border:1px solid #fff;}
.lsr-storm {color:#ffe66b;text-shadow:2px 2px #7c4a00;
 background:linear-gradient(135deg,#4c428e,#25244b);border:1px solid #b5a4ff;}
.lsr-other {color:#fff;background:#556779;border:1px solid #c4d1df;}
/* Compact colored report markers at statewide zoom; full weather words on zoom-in. */
.lsr-compact .lsr-label {font-size:0 !important;letter-spacing:0;padding:0;width:13px;height:13px;
 box-sizing:border-box;border-radius:50%;transform:translate(-50%,-50%) !important;
 text-shadow:none !important;filter:drop-shadow(0 1px 2px #111) !important;}
.lsr-compact .lsr-label::after {display:none;}
.lsr-compact .lsr-label:hover {outline:2px solid #fff;outline-offset:2px;}
</style>
"""


class LSRZoomMarkers(MacroElement):
    """Switch only LSR icons between dots and full labels as Leaflet zoom changes."""
    _template = Template(r"""
    {% macro script(this, kwargs) %}
    (function () {
      var map = {{this._parent.get_name()}};
      function updateLabels() {
        var container = map.getContainer();
        if (map.getZoom() <= 7) container.classList.add('lsr-compact');
        else container.classList.remove('lsr-compact');
      }
      map.on('zoomend', updateLabels);
      updateLabels();
    })();
    {% endmacro %}
    """)
    def __init__(self):
        super().__init__()
        self._name = 'LSRZoomMarkers'


def lsr_map(reports):
    m = folium.Map(location=[36.0, -102.0] if TEST_THREE_STATES else [38.5, -98], zoom_start=6 if TEST_THREE_STATES else 7,
                   tiles='OpenStreetMap', control_scale=True)
    # The CSS is inserted into the Folium document header, not the Streamlit page.
    from branca.element import Element
    m.get_root().header.add_child(Element(LSR_MARKER_CSS))
    groups = {}
    mapped = 0
    for report in reports:
        props = report.get('attributes') or {}
        geom = report.get('geometry') or {}
        lon, lat = geom.get('x'), geom.get('y')
        if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
            continue
        label, style, category_name = lsr_style(props.get('descript'))
        if style == 'hail':
            label = hail_size_label(props.get('magnitude'), props.get('units')) or 'HAIL'
        if category_name not in groups:
            groups[category_name] = folium.FeatureGroup(name=category_name, show=True)
            groups[category_name].add_to(m)
        magnitude = ' '.join(str(props.get(k) or '').strip() for k in ('magnitude', 'units')).strip()
        occurred = central_time(props.get('valid_time'))
        fields = [('Event', props.get('descript')), ('Location', props.get('loc_desc')),
                  ('Magnitude', magnitude), ('NWS office', props.get('wfo_id')),
                  ('Remarks', props.get('remarks'))]
        popup_html = ('<div style="width:300px;font:14px Arial,sans-serif">'
                      '<div style="padding:9px;background:#e7f2fc;border-radius:5px;">'
                      '<strong>EVENT OCCURRED</strong><div style="font-size:17px;font-weight:bold;">'
                      + html.escape(occurred) + '</div></div>' + ''.join(
            f'<p><strong>{html.escape(label)}:</strong> {html.escape(str(value))}</p>'
            for label, value in fields if value not in (None, '')) + '</div>')
        # Escape the report-specific hail diameter before inserting it in marker HTML.
        icon_html = f'<span class="lsr-label lsr-{style}">{html.escape(label)}</span>'
        folium.Marker(
            location=[lat, lon],
            icon=folium.DivIcon(html=icon_html, icon_size=(0, 0), icon_anchor=(0, 0),
                                class_name='lsr-custom-icon'),
            tooltip=f"{props.get('descript') or 'Report'} — {occurred} — {props.get('loc_desc') or 'Region'}",
            popup=folium.Popup(popup_html, max_width=340),
        ).add_to(groups[category_name])
        mapped += 1
    folium.LayerControl(collapsed=True).add_to(m)
    m.add_child(LSRZoomMarkers())
    m.fit_bounds(MAP_BOUNDS)
    return m, mapped


@st.fragment(run_every='30min')
def local_storm_reports_panel():
    st.divider()
    st.subheader('📍 ' + REGION_LABEL + ' Local Storm Reports — Last 24 Hours')
    st.caption('Separate map of preliminary storm reports from NWS offices. '
               'NOAA updates this service approximately every 30 minutes; '
               'new reports may appear with a delay.')
    try:
        reports, retrieved = regional_storm_reports()
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        st.error(f'NOAA Local Storm Reports could not be loaded: {exc}. '
                 'This does not mean there were no storm reports.')
        return
    st.caption('Last successful report retrieval: ' +
               central_time(retrieved))
    st.metric('Regional storm reports (24 hours)', len(reports))
    m, mapped = lsr_map(reports)
    st_folium(m, width='stretch', height=530, returned_objects=[], key='ks_lsr_map')
    if not reports:
        st.info('NOAA currently returned no storm reports for the selected states in its 24-hour layer.')
    elif mapped < len(reports):
        st.warning(f'{len(reports)-mapped} reports had no usable map coordinates.')
    st.caption('Hail markers show reported diameter in inches when NOAA supplies a valid size; otherwise they read HAIL. Click a weather-styled word marker to see when the event occurred (Central Time), its location, magnitude and remarks. Reports are preliminary and may change. At statewide zoom, reports appear as compact colored dots. Zoom in to see the full weather-styled words. Click either marker for report details.')
    st.caption('Source: NOAA/NWS Local Storm Reports ArcGIS service, layer 0 (Last_24_Hours).')


local_storm_reports_panel()


st.subheader('Local weather forecast')
try:
    daily = nws_forecast(point['forecast'])
    hourly = nws_forecast(point['forecastHourly'])
except (requests.RequestException, KeyError, TypeError) as exc:
    st.error(f'Forecasts could not be loaded: {exc}')
else:
    with st.expander('Daily forecast', expanded=True):
        cols = st.columns(2)
        for i, p in enumerate(daily):
            with cols[i % 2]:
                with st.container(border=True):
                    st.markdown(f"**{p.get('name', 'Forecast')}**")
                    st.metric('Temperature', f"{p.get('temperature', '—')}°{p.get('temperatureUnit', 'F')}")
                    st.write(p.get('shortForecast') or 'Forecast unavailable')
                    st.caption(f"Wind: {p.get('windSpeed', '—')} {p.get('windDirection', '')}")
                    if p.get('detailedForecast'):
                        st.write(p['detailedForecast'])
    with st.expander('Hourly forecast'):
        count = st.slider('Hours to display', 6, 48, 24, 6)
        rows = []
        for p in hourly[:count]:
            pop = (p.get('probabilityOfPrecipitation') or {}).get('value')
            rows.append({'Time': central_time(p.get('startTime')),
                         'Temp': f"{p.get('temperature', '—')}°{p.get('temperatureUnit', 'F')}",
                         'Conditions': p.get('shortForecast', '—'),
                         'Rain chance': f'{pop}%' if pop is not None else '—',
                         'Wind': f"{p.get('windSpeed', '—')} {p.get('windDirection', '')}"})
        st.dataframe(rows, width='stretch', hide_index=True)

st.divider()
st.markdown('[Radar imagery by RainViewer](https://www.rainviewer.com/) · [Official NWS radar](https://radar.weather.gov/)')
st.caption('All displayed times use U.S. Central Time (CST/CDT), adjusted automatically for daylight saving time.')
st.caption('Sources: U.S. National Weather Service API (weather.gov), RainViewer radar (rainviewer.com), Zippopotam.us, OpenStreetMap. Data and network availability may be delayed. No guarantees of continuous monitoring or alert delivery. Not affiliated with NOAA or the NWS.')
