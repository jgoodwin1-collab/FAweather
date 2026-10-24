import re
import csv
import io
import math
from datetime import date
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
# TEMPORARY LSR HISTORY: set False to restore the original 24-hour reports.
TEST_LSR_72_HOURS = True
LSR_HOURS = 72 if TEST_LSR_72_HOURS else 24
LSR_LAYER_ID = 2 if TEST_LSR_72_HOURS else 0
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
if TEST_LSR_72_HOURS:
    st.info('TEMPORARY LSR TEST: Showing 72 hours of storm reports. Change TEST_LSR_72_HOURS to False to restore 24 hours.')
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


# Independent NOAA Local Storm Reports map (24-hour or 72-hour service layer).
LSR_URL = ('https://mapservices.weather.noaa.gov/vector/rest/services/'
           f'obs/nws_local_storm_reports/MapServer/{LSR_LAYER_ID}/query')


@st.cache_data(ttl=1800, show_spinner=False)
def regional_storm_reports():
    """Read reports for configured states from NOAA selected rolling-hours layer."""
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



def wind_speed_label(magnitude, units):
    """Show a reported wind speed in mph; never infer speed from damage text.

    ArcGIS magnitude is normally numeric with a separate units field.
    Convert knots, km/h and m/s when explicitly identified.
    """
    if magnitude is None or str(magnitude).strip() == '':
        return None
    raw = str(magnitude).strip().replace(',', '')
    match = re.fullmatch(
        r'([0-9]+(?:\.[0-9]+)?)\s*(mph|mi/h|kt|kts|knots?|km/h|kph|m/s)?',
        raw, flags=re.I)
    if not match:
        return None
    speed = float(match.group(1))
    inline_unit = (match.group(2) or '').lower()
    field_unit = str(units or '').strip().lower()
    unit = inline_unit or field_unit
    if unit in ('', 'mph', 'mi/h', 'miles per hour', 'mile per hour'):
        pass
    elif unit in ('kt', 'kts', 'knot', 'knots'):
        speed *= 1.150779448
    elif unit in ('km/h', 'kph', 'kmh', 'kilometers per hour'):
        speed *= 0.621371192
    elif unit in ('m/s', 'meters per second'):
        speed *= 2.236936292
    else:
        return None
    if not 0 < speed <= 350:
        return None
    return f'{speed:.0f}mph'


def lsr_style(event):
    """Choose short readable labels and weather-inspired marker classes."""
    event = (event or '').lower()
    if 'funnel cloud' in event or 'funnel-cloud' in event:
        return 'FUNNEL', 'funnel', 'Funnel cloud'
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
/* Hail diameter: exact original FLOOD styling, including its blue background. */
.lsr-hail {color:#d3faff;text-shadow:0 1px #115a93,0 2px #07385b;
 background:linear-gradient(145deg,#42c6ed,#0874b6 65%,#045177);border:1px solid #8fe5ff;}
.lsr-fog {color:#d9dce0;text-shadow:0 0 4px #fff,0 0 8px #bfc4ce,0 0 13px #999;
 background:rgba(77,87,99,.78);border:1px solid #a6b0b9;filter:blur(.35px) drop-shadow(0 2px 2px #111);}
/* Keep RAIN unchanged; make FLOOD distinctly darker than the hail labels. */
.lsr-rain {color:#d3faff;text-shadow:0 1px #115a93,0 2px #07385b;
 background:linear-gradient(145deg,#42c6ed,#0874b6 65%,#045177);border:1px solid #8fe5ff;}
.lsr-flood {color:#e3f7ff;text-shadow:0 1px #06385c,0 2px #04223d;
 background:linear-gradient(145deg,#2475b6,#104578 65%,#082a4d);border:1px solid #6bbce9;}
.lsr-tornado {color:#fff2f2;text-shadow:1px 2px #7c1727,2px 3px #450813;
 background:linear-gradient(120deg,#f85165,#9b172c);border:1px solid #ffadb5;transform:translate(-50%,-50%) skew(-6deg);}
.lsr-wind {color:#fff2d4;text-shadow:2px 1px #95501b;
 background:linear-gradient(110deg,#f7bd64,#bf681f);border:1px solid #ffdc9a;transform:translate(-50%,-50%) skew(-8deg);}
.lsr-snow,.lsr-ice {color:#f5fcff;text-shadow:1px 2px #638bb6;
 background:linear-gradient(135deg,#d2edff,#809fc9);border:1px solid #fff;}
.lsr-storm {color:#ffe66b;text-shadow:2px 2px #7c4a00;
 background:linear-gradient(135deg,#4c428e,#25244b);border:1px solid #b5a4ff;}
/* Gray funnel-cloud marker with a simple cloud and tapered funnel silhouette. */
.lsr-funnel {color:#fff;background:#556779;border:1px solid #c4d1df;min-width:34px;text-align:center;}
.lsr-funnel svg {display:block;width:34px;height:29px;overflow:visible;}
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


# Display only report types actually plotted on the map.
LSR_LEGEND_COLORS = {
    'hail': '#0874b6', 'wind': '#bf681f', 'flood': '#104578',
    'rain': '#0874b6', 'tornado': '#b52840', 'funnel': '#556779',
    'fog': '#59626d', 'snow': '#809fc9', 'ice': '#809fc9',
    'storm': '#4c428e', 'other': '#556779',
}

FUNNEL_SVG = (
    '<svg viewBox="0 0 42 34" xmlns="http://www.w3.org/2000/svg" aria-label="Funnel cloud" role="img">'
    '<path d="M8 13 C2 13 2 4 11 5 C13 -1 24 -1 27 5 C37 3 41 13 33 16 L11 16 Z" '
    'fill="#e6ebf1" stroke="#a7b2bf" stroke-width="1.5"/>'
    '<path d="M12 16 C16 19 30 17 30 16 C27 21 25 23 23 25 C21 27 23 30 19 33 '
    'C19 28 15 25 17 23 C18 21 14 20 12 16 Z" fill="#d3dbe5" stroke="#9ba9b9" stroke-width="1"/>'
    '</svg>'
)


def lsr_map(reports):
    m = folium.Map(location=[36.0, -102.0] if TEST_THREE_STATES else [38.5, -98], zoom_start=6 if TEST_THREE_STATES else 7,
                   tiles='OpenStreetMap', control_scale=True)
    # The CSS is inserted into the Folium document header, not the Streamlit page.
    from branca.element import Element
    m.get_root().header.add_child(Element(LSR_MARKER_CSS))
    groups = {}
    mapped = 0
    legend_counts = {}
    for report in reports:
        props = report.get('attributes') or {}
        geom = report.get('geometry') or {}
        lon, lat = geom.get('x'), geom.get('y')
        if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
            continue
        label, style, category_name = lsr_style(props.get('descript'))
        if style == 'hail':
            label = hail_size_label(props.get('magnitude'), props.get('units')) or 'HAIL'
        elif style == 'wind':
            label = wind_speed_label(props.get('magnitude'), props.get('units')) or 'Damage'
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
        # Escape report-specific marker text before inserting it in marker HTML.
        marker_content = FUNNEL_SVG if style == 'funnel' else html.escape(label)
        icon_html = f'<span class="lsr-label lsr-{style}">{marker_content}</span>' 
        folium.Marker(
            location=[lat, lon],
            icon=folium.DivIcon(html=icon_html, icon_size=(0, 0), icon_anchor=(0, 0),
                                class_name='lsr-custom-icon'),
            tooltip=f"{props.get('descript') or 'Report'} — {occurred} — {props.get('loc_desc') or 'Region'}",
            popup=folium.Popup(popup_html, max_width=340),
        ).add_to(groups[category_name])
        mapped += 1
        legend_key = (category_name, style)
        legend_counts[legend_key] = legend_counts.get(legend_key, 0) + 1
    folium.LayerControl(collapsed=True).add_to(m)
    m.add_child(LSRZoomMarkers())
    m.fit_bounds(MAP_BOUNDS)
    return m, mapped, legend_counts


@st.fragment(run_every='30min')
def local_storm_reports_panel():
    st.divider()
    st.subheader('📍 ' + REGION_LABEL + f' Local Storm Reports — Last {LSR_HOURS} Hours')
    st.caption(f'Separate map of preliminary storm reports from NWS offices, covering the last {LSR_HOURS} hours. '
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
    st.metric(f'Regional storm reports ({LSR_HOURS} hours)', len(reports))
    m, mapped, legend_counts = lsr_map(reports)
    if legend_counts:
        legend_parts = []
        for (name, style), count in sorted(legend_counts.items(), key=lambda item: item[0][0]):
            color = LSR_LEGEND_COLORS.get(style, '#556779')
            legend_parts.append(
                '<span style="display:inline-flex;align-items:center;gap:6px;'
                'padding:5px 9px;margin:3px 5px 3px 0;background:#182a3f;'
                'border:1px solid #3a4b60;border-radius:6px;color:#f3f7ff;'
                'font:13px Arial,sans-serif;white-space:nowrap">'
                f'<span style="display:inline-block;width:13px;height:13px;'
                f'background:{color};border:1px solid #b9c7d6;border-radius:3px"></span>'
                f'{html.escape(name)} ({count})</span>'
            )
        st.markdown('**Reports currently displayed — legend**')
        st.markdown(''.join(legend_parts), unsafe_allow_html=True)
    st_folium(m, width='stretch', height=530, returned_objects=[], key='ks_lsr_map')
    if not reports:
        st.info(f'NOAA currently returned no storm reports for the selected states in its {LSR_HOURS}-hour layer.')
    elif mapped < len(reports):
        st.warning(f'{len(reports)-mapped} reports had no usable map coordinates.')
    st.caption('Hail markers show reported diameter in inches using the original blue FLOOD style. Wind markers retain their existing styling and show reported wind speed in mph, or Damage when no usable speed is supplied. Click a weather-styled word marker to see when the event occurred (Central Time), its location, magnitude and remarks. Reports are preliminary and may change. At statewide zoom, reports appear as compact colored dots. Zoom in to see the full weather-styled words. Click either marker for report details.')
    st.caption(f'Source: NOAA/NWS Local Storm Reports ArcGIS service, layer {LSR_LAYER_ID} (Last_{LSR_HOURS}_Hours).')


local_storm_reports_panel()




# Third, independent CoCoRaHS rainfall map. Only Kansas is requested from this
# source, even when the separate LSR map is in three-state testing mode.
# This is a fixed historical report date, NOT a rolling 24-hour total.
COCORAHS_REPORT_DATE = '10/14/2026'  # MM/DD/YYYY; edit for another day.
COCORAHS_EXPORT_URL = 'https://data.cocorahs.org/export/exportreports.aspx'


def _rain_field(row, *aliases):
    """Match CoCoRaHS CSV headers despite spaces/case/punctuation changes."""
    normalized = {re.sub(r'[^a-z0-9]', '', str(k).lower()): v for k, v in row.items() if k}
    for alias in aliases:
        value = normalized.get(re.sub(r'[^a-z0-9]', '', alias.lower()))
        if value is not None and str(value).strip():
            return str(value).strip()
    return ''


def _rain_amount(raw):
    raw = str(raw or '').strip().replace(',', '')
    # T=trace is not 0.00; keep its distinct meaning in popups/labels.
    if raw.upper() in ('T', 'TRACE'):
        return 0.0, 'Trace'
    if not raw or raw.upper() in ('NA', 'N/A', 'M', 'NULL', 'NONE', '-9999'):
        return None, None
    try:
        amount = float(raw)
    except ValueError:
        return None, None
    if not math.isfinite(amount) or amount < 0 or amount > 100:
        return None, None
    return amount, f'{amount:.2f}"'


def _parse_cocorahs_csv(body):
    if not body.strip():
        raise ValueError('CoCoRaHS returned an empty CSV.')
    if body.lstrip().lower().startswith(('<!doctype', '<html')):
        raise ValueError('CoCoRaHS returned a web page rather than CSV data.')
    # Some exports have descriptive lines preceding the header.
    lines = body.lstrip('\ufeff').splitlines()
    header_idx = next((i for i, line in enumerate(lines[:35])
                       if 'latitude' in line.lower() and 'longitude' in line.lower()), None)
    if header_idx is None:
        raise ValueError('CoCoRaHS CSV does not contain Latitude/Longitude headers.')
    reader = csv.DictReader(io.StringIO('\n'.join(lines[header_idx:])))
    reports = []
    for row in reader:
        try:
            lat = float(_rain_field(row, 'Latitude', 'Lat'))
            lon = float(_rain_field(row, 'Longitude', 'Lon', 'Long'))
        except ValueError:
            continue
        # Kansas bounding check protects against non-Kansas export rows.
        if not (36.98 <= lat <= 40.02 and -102.07 <= lon <= -94.55):
            continue
        state = _rain_field(row, 'State', 'StateCode', 'StateAbbreviation')
        if state and state.upper() not in ('KS', 'KANSAS'):
            continue
        raw_amount = _rain_field(row, 'TotalPrecipAmt', 'TotalPrecipitation',
                                 'Precipitation', 'PrecipAmount', 'PrecipAmt',
                                 'GaugeCatch', 'GaugeCatchIn', '24hrPrecip',
                                 'TotalPrecip', 'Precip', 'Rainfall')
        amount, detail = _rain_amount(raw_amount)
        if amount is None:
            continue
        reports.append({
            'lat': lat, 'lon': lon, 'amount': amount, 'detail': detail,
            'station': _rain_field(row, 'StationNumber', 'StationID', 'StationCode'),
            'name': _rain_field(row, 'StationName', 'Station'),
            'county': _rain_field(row, 'County', 'CountyName'),
            'obs_time': _rain_field(row, 'ObservationTime', 'ObsTime', 'ObservationDate', 'ObsDate'),
        })
    return reports


@st.cache_data(ttl=1800, show_spinner=False)
def cocorahs_daily_reports(report_date):
    params = {'ReportType': 'Daily', 'dtf': '1', 'Format': 'CSV',
              'State': 'KS', 'ReportDateType': 'reportdate',
              'Date': report_date, 'TimesInGMT': 'False'}
    response = requests.get(COCORAHS_EXPORT_URL, params=params,
                            headers={'User-Agent': USER_AGENT, 'Accept': 'text/csv,*/*'},
                            timeout=45)
    response.raise_for_status()
    reports = _parse_cocorahs_csv(response.text)
    return reports, datetime.now(timezone.utc).isoformat()


RAIN_MARKER_CSS = r"""<style>
.coco-rain {display:inline-block;position:relative;transform:translate(-50%,-50%);
  padding:4px 7px;white-space:nowrap;font:900 15px Arial,sans-serif;
  color:#ffffff;background:#126da6;border:1px solid #a5e8ff;
  border-radius:7px;text-shadow:0 1px 1px #003a60;
  box-shadow:0 2px 4px #0008;cursor:pointer;}
.coco-rain::after {content:'';position:absolute;bottom:-4px;left:50%;width:6px;height:6px;
  border-radius:50%;background:#ffffff;transform:translateX(-50%);}
/* At statewide zoom use small dots; expand into readable totals when zoomed in. */
.coco-compact .coco-rain {font-size:0 !important;line-height:0;padding:0;width:11px;height:11px;
  border-radius:50%;box-sizing:border-box;transform:translate(-50%,-50%);text-shadow:none;}
.coco-compact .coco-rain::after {display:none;}
.coco-compact .coco-rain:hover {outline:2px solid white;outline-offset:2px;}
</style>"""


class RainZoomLabels(MacroElement):
    _template = Template(r"""
    {% macro script(this, kwargs) %}
    (function() {
      var map = {{this._parent.get_name()}};
      function sync() {
        var el = map.getContainer();
        if (map.getZoom() <= 8) el.classList.add('coco-compact');
        else el.classList.remove('coco-compact');
      }
      map.on('zoomend', sync); sync();
    })();
    {% endmacro %}
    """)

    def __init__(self):
        super().__init__()
        self._name = 'RainZoomLabels'


# Experimental rainfall analysis. Interpolation is limited to the observation
# network; colored areas are estimates, NOT measured rainfall at every point.
def rainfall_contour_overlay(reports, show_minor=False):
    """Subtle contours with a gauge-distance mask (not extrapolated measurements)."""
    import base64
    import io as _io
    import numpy as np
    from scipy.interpolate import LinearNDInterpolator
    from scipy.spatial import cKDTree, QhullError
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.colors import ListedColormap, BoundaryNorm

    locations = {}
    for r in reports:
        key = (round(r['lon'], 5), round(r['lat'], 5))
        locations.setdefault(key, []).append(r['amount'])
    if len(locations) < 3:
        return None, 'At least three distinct stations are needed for interpolation.'
    points = np.asarray(list(locations), dtype=float)
    values = np.asarray([sum(v) / len(v) for v in locations.values()], dtype=float)
    if max(values) < 0.25:
        return None, 'All totals are below 0.25 inch; no contour thresholds were reached.'
    try:
        interp = LinearNDInterpolator(points, values, fill_value=np.nan)
    except (QhullError, ValueError):
        return None, 'Station positions do not support a valid triangulation.'

    west, east, south, north = -102.05, -94.59, 36.99, 40.01
    nx, ny = 850, 450
    xs = np.linspace(west, east, nx)
    ys = np.linspace(south, north, ny)
    xx, yy = np.meshgrid(xs, ys)
    rainfall = interp(xx, yy)

    # Approximate local planar miles (latitude and longitude have different scales).
    # Mask grid cells >35 miles from the nearest reporting station; this is a
    # visualization coverage heuristic, not a quantified uncertainty interval.
    lat_scale = 69.0
    lon_scale = 69.0 * np.cos(np.deg2rad(38.5))
    scaled_stations = points * [lon_scale, lat_scale]
    scaled_grid = np.column_stack((xx.ravel() * lon_scale, yy.ravel() * lat_scale))
    nearest_miles, _ = cKDTree(scaled_stations).query(scaled_grid)
    rainfall[nearest_miles.reshape(xx.shape) > 35.0] = np.nan
    rainfall = np.ma.masked_invalid(rainfall)
    if rainfall.count() == 0:
        return None, 'No areas are close enough to reporting gauges for interpolation.'

    thresholds = [0.25, 0.5, 1.0]
    thresholds += [float(i) for i in range(2, max(3, int(np.ceil(max(values))) + 1))]
    levels = [0.0] + thresholds + [max(float(max(values)) + 1, thresholds[-1] + 1)]
    # Restrained palette: light blues to blue, green, gold, muted red/purple.
    palette = ['#e8f3fa', '#ccebf6', '#80c5ec', '#338fd0', '#40a876',
               '#e6b54e', '#d98954', '#b85b68', '#9363a0', '#77558e']
    if len(levels) - 1 > len(palette):
        palette += [palette[-1]] * (len(levels) - 1 - len(palette))
    cmap = ListedColormap(palette[:len(levels)-1])
    cmap.set_bad((0, 0, 0, 0))
    norm = BoundaryNorm(levels, cmap.N)

    fig = Figure(figsize=(10, 5), dpi=130, facecolor='none')
    FigureCanvasAgg(fig)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(west, east)
    ax.set_ylim(south, north)
    ax.axis('off')
    ax.contourf(xx, yy, rainfall, levels=levels, cmap=cmap, norm=norm,
                antialiased=True, alpha=0.40)
    # Statewide view: only 1-inch interval lines. Minor thresholds are reserved
    # for zoomed-in viewing; the raster is switched without rebuilding the map.
    visible_levels = ([v for v in thresholds if v <= float(rainfall.max())]
                      if show_minor else
                      [v for v in thresholds if v >= 1.0 and v <= float(rainfall.max())])
    if visible_levels:
        ax.contour(xx, yy, rainfall, levels=visible_levels,
                   colors='#285c76', linewidths=0.65 if show_minor else 0.85,
                   alpha=0.65 if show_minor else 0.72)
    png = _io.BytesIO()
    fig.savefig(png, format='png', dpi=130, transparent=True, pad_inches=0)
    uri = 'data:image/png;base64,' + base64.b64encode(png.getvalue()).decode('ascii')
    return (uri, [[south, west], [north, east]]), None


class RainContourZoom(MacroElement):
    """Show minor 0.25/0.50 contours only at neighborhood zoom levels."""
    _template = Template(r"""
    {% macro script(this, kwargs) %}
    (function() {
      var map = {{this._parent.get_name()}};
      var major = {{this.major}};
      var detail = {{this.detail}};
      function sync() {
        if (!map.hasLayer(major) && !map.hasLayer(detail)) return;
        if (map.getZoom() >= 9) {
          if (map.hasLayer(major)) map.removeLayer(major);
          if (!map.hasLayer(detail)) map.addLayer(detail);
        } else {
          if (map.hasLayer(detail)) map.removeLayer(detail);
          if (!map.hasLayer(major)) map.addLayer(major);
        }
      }
      map.on('zoomend', sync);
      sync();
    })();
    {% endmacro %}
    """)

    def __init__(self, major, detail):
        super().__init__()
        self._name = 'RainContourZoom'
        self.major = major.get_name()
        self.detail = detail.get_name()


def make_cocorahs_map(reports, display_mode="Contours + observations"):
    from branca.element import Element
    m = folium.Map(location=[38.5, -98], zoom_start=7,
                   tiles='OpenStreetMap', control_scale=True)
    m.get_root().header.add_child(Element(RAIN_MARKER_CSS))
    contour_note = None
    if display_mode == 'Contours + observations':
        try:
            major, contour_note = rainfall_contour_overlay(reports, show_minor=False)
            detail, _ = rainfall_contour_overlay(reports, show_minor=True)
        except Exception as exc:
            major, detail = None, None
            contour_note = f'Contour analysis unavailable: {type(exc).__name__}'
        if major and detail:
            major_layer = folium.raster_layers.ImageOverlay(
                image=major[0], bounds=major[1], name='Estimated rainfall contours',
                opacity=1, interactive=False, show=True).add_to(m)
            detail_layer = folium.raster_layers.ImageOverlay(
                image=detail[0], bounds=detail[1], name='Detailed rainfall contours',
                opacity=1, interactive=False, show=False).add_to(m)
            m.add_child(RainContourZoom(major_layer, detail_layer))
    layer = folium.FeatureGroup(name='CoCoRaHS rainfall totals', show=True).add_to(m)
    for item in reports:
        amount = item['amount']
        # Only the marker is rounded. The popup retains hundredths of an inch.
        label = 'T' if item['detail'] == 'Trace' else f'{amount:.1f}"'
        popup_fields = [
            ('Rainfall', item['detail']), ('Station', item['station']),
            ('Station name', item['name']), ('County', item['county']),
            ('Observation time (as supplied)', item['obs_time']),
            ('Report date', COCORAHS_REPORT_DATE),
        ]
        popup = '<div style="width:250px;font:14px Arial,sans-serif"><strong>CoCoRaHS Daily Rainfall</strong>'
        popup += ''.join(f'<p><b>{html.escape(k)}:</b> {html.escape(str(v))}</p>'
                         for k, v in popup_fields if v)
        popup += '</div>'
        folium.Marker(
            [item['lat'], item['lon']],
            icon=folium.DivIcon(html=f'<span class="coco-rain">{html.escape(label)}</span>',
                                icon_size=(0, 0), icon_anchor=(0, 0),
                                class_name='coco-custom-icon'),
            tooltip=f'{label} — {item["name"] or item["station"] or "Rainfall station"}',
            popup=folium.Popup(popup, max_width=310),
        ).add_to(layer)
    folium.LayerControl(collapsed=True).add_to(m)
    m.add_child(RainZoomLabels())
    m.fit_bounds([[36.99, -102.05], [40.01, -94.59]])
    return m, contour_note


@st.fragment(run_every='30min')
def cocorahs_rainfall_panel():
    st.divider()
    st.subheader('🌧️ Kansas CoCoRaHS Rainfall Reports')
    st.caption(f'Daily gauge reports for {COCORAHS_REPORT_DATE} (report date). '
               'This is a historical daily report map, not a live radar or a rolling 24-hour rainfall estimate.')
    try:
        reports, retrieved = cocorahs_daily_reports(COCORAHS_REPORT_DATE)
    except (requests.RequestException, ValueError, csv.Error) as exc:
        st.warning(f'CoCoRaHS rainfall data could not be loaded: {exc}. '
                   'The radar and Local Storm Reports maps are unaffected.')
        st.markdown(f'[View CoCoRaHS data exports]({COCORAHS_EXPORT_URL})')
        return
    st.caption('Data retrieved: ' + central_time(retrieved))
    st.metric('Kansas rainfall observations mapped', len(reports))
    if not reports:
        st.info('No mappable Kansas rainfall observations were returned for this date. '
                'Check the CSV column names or choose another report date.')
        return
    st.caption('Zoomed out: blue station dots. Zoom in past level 8: labels show rainfall '
               'rounded to one decimal inch. Click a marker for the reported value '
               'to two decimals and station details. T indicates trace precipitation.')
    display_mode = st.radio('Rainfall map display',
                            ['Contours + observations', 'Observed totals only'],
                            horizontal=True, key='cocorahs_display_mode')
    rain_map, contour_note = make_cocorahs_map(reports, display_mode)
    if contour_note:
        st.info(contour_note)
    st.caption('**Estimated contour thresholds:** 0.25", 0.50", 1.0", then 2.0", '
               '3.0", and upward by 1". At statewide zoom, only 1-inch contour lines '
               'are drawn; zoom to level 9+ to reveal the 0.25" and 0.50" lines. '
               'Colored areas are deliberately translucent (40%).')
    st.markdown('**Estimated rainfall color key (inches):**')
    st.markdown('🟦 **0–0.25** · 🩵 **0.25–0.50** · 🔵 **0.50–1.0** · '
                '🔵 **1.0–2.0** · 🟢 **2.0–3.0** · 🟡 **3.0–4.0** · '
                '🟠 **4.0–5.0** · 🔴 **5.0–6.0** · 🟣 **6.0+**')
    st_folium(rain_map, width='stretch', height=550,
              returned_objects=[], key='ks_cocorahs_rainfall_map')
    st.caption('Shading is an interpolated estimate, not an observed total. Areas more than '
               'approximately 35 miles from the nearest reporting gauge, or outside the '
               'station triangulation, are left transparent. The 35-mile cutoff is a display '
               'rule, not a statistical confidence bound; local extremes can be missed.')
    st.caption('Source: CoCoRaHS volunteer daily precipitation observations. '
               'Reports are station measurements and may not represent conditions between gauges.')


cocorahs_rainfall_panel()


# Fourth independent map: experimental rainfall-runoff scenario laboratory.
# This first pilot does NOT model inundation depth, stream stage, road overtopping,
# bridge/culvert hydraulics, or delineate a verified watershed boundary.
# All calculations are deliberately labeled illustrative screening only.


# USGS NLDI watershed upstream of the Topeka streamgage (not the entire creek basin).
BASIN_GAGE_ID = '06889630'
BASIN_URL = ('https://api.water.usgs.gov/nldi/linked-data/nwissite/'
             f'USGS-{BASIN_GAGE_ID}/basin')


@st.cache_data(ttl=86400, show_spinner=False)
def shunganunga_basin():
    response = requests.get(BASIN_URL,
                            params={'f': 'json', 'simplified': 'true', 'splitCatchment': 'true'},
                            headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'},
                            timeout=35)
    response.raise_for_status()
    data = response.json()
    if data.get('type') == 'FeatureCollection':
        features = data.get('features') or []
        if not features:
            raise ValueError('USGS returned no watershed polygon.')
        geom = features[0].get('geometry')
    elif data.get('type') == 'Feature':
        geom = data.get('geometry')
    else:
        geom = data
    if not isinstance(geom, dict) or geom.get('type') not in ('Polygon', 'MultiPolygon'):
        raise ValueError('USGS did not return a polygon geometry.')
    return geom


def flood_scenario_map(center_lat, center_lon, radius_miles, rain_inches, duration_hours,
                       runoff_fraction, crossing_restriction, basin_geometry=None, show_circle=True):
    m = folium.Map(location=[center_lat, center_lon], zoom_start=11,
                   tiles='OpenStreetMap', control_scale=True)
    if basin_geometry:
        folium.GeoJson(
            basin_geometry, name='USGS watershed above gage 06889630',
            style_function=lambda _: {'color': '#f2a33b', 'weight': 4,
                                      'fillColor': '#e7a03d', 'fillOpacity': 0.10},
            tooltip='USGS NLDI upstream drainage basin — NOT an inundation extent',
        ).add_to(m)
    # Optional circle represents hypothetical rainfall footprint, NOT flooding.
    if show_circle:
        folium.Circle(
        location=[center_lat, center_lon], radius=radius_miles * 1609.344,
        color='#46b4ee', weight=2, fill=True, fill_color='#56b9ed',
        fill_opacity=0.17,
        tooltip='Illustrative uniform-rainfall footprint (not a watershed or inundation extent)',
        popup=folium.Popup(
            f'<b>Hypothetical rainfall footprint</b><br>{rain_inches:.1f} inches over '
            f'{duration_hours:g} hours<br>Radius: {radius_miles:g} miles<br>'
            'This is NOT a mapped flood extent.', max_width=300)
    ).add_to(m)
    folium.Marker(
        [center_lat, center_lon],
        tooltip='Scenario center — approximate Shunganunga Creek / Topeka area',
        popup='Scenario center only. Move the latitude/longitude controls to explore other locations.',
        icon=folium.Icon(color='blue', icon='tint', prefix='fa')
    ).add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)
    if basin_geometry:
        try:
            # Folium bounds of the polygon (including MultiPolygon geometry).
            coords = []
            def collect(node):
                if isinstance(node, list):
                    if len(node) >= 2 and all(isinstance(v, (int, float)) for v in node[:2]):
                        coords.append(node[:2])
                    else:
                        for item in node:
                            collect(item)
            collect(basin_geometry['coordinates'])
            if coords:
                m.fit_bounds([[min(y for x, y in coords), min(x for x, y in coords)],
                              [max(y for x, y in coords), max(x for x, y in coords)]])
        except (ValueError, KeyError, TypeError):
            pass
    else:
        m.fit_bounds([[center_lat - radius_miles / 69 * 1.35,
                       center_lon - radius_miles / 54 * 1.35],
                      [center_lat + radius_miles / 69 * 1.35,
                       center_lon + radius_miles / 54 * 1.35]])
    return m


@st.fragment

def experimental_flood_panel():
    st.divider()
    st.subheader('🧪 Kansas Flood Simulator — Shunganunga Creek pilot')
    st.warning('EXPERIMENTAL SCENARIO ONLY — This is NOT a flood-inundation map, '
               'road-closure prediction, or safety assessment. Do not use it to decide '
               'whether any road is safe to travel.')
    st.caption('Fourth independent map. Orange outline: actual upstream basin from USGS when available. '
               'Blue circle: optional hypothetical rainfall footprint. Neither represents flood extent.')
    try:
        basin_geometry = shunganunga_basin()
        st.success('USGS NLDI watershed boundary loaded for streamgage 06889630.')
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        basin_geometry = None
        st.warning('USGS watershed boundary unavailable at this moment; only the hypothetical '
                   f'rainfall footprint can be displayed. Details: {type(exc).__name__}: {exc}')
    show_circle = st.checkbox('Show hypothetical rainfall circle', value=True,
                              key='flood_show_circle')
    left, right = st.columns(2)
    with left:
        rain = st.slider('Hypothetical rainfall (inches)', 0.5, 15.0, 12.0, 0.5,
                         key='flood_rain')
        duration = st.select_slider('Rainfall duration (hours)',
                                    options=[1, 2, 3, 6, 12, 24], value=3,
                                    key='flood_duration')
        radius = st.slider('Illustrative rainfall footprint radius (miles)',
                           1.0, 15.0, 5.0, 0.5, key='flood_radius')
    with right:
        soil = st.selectbox('Assumed initial runoff conditions',
                            ['Dry (20% runoff)', 'Typical (45% runoff)',
                             'Saturated (75% runoff)'], index=1,
                            key='flood_soil')
        restriction = st.checkbox('Hypothetical restricted crossing / debris blockage',
                                  key='flood_restriction')
        st.caption('Restriction is a scenario flag only. No bridge or culvert capacity '
                   'data have been loaded; it does not alter computed water depth.')
    with st.expander('Adjust scenario center (illustrative Topeka starting point)'):
        lat = st.number_input('Latitude', min_value=36.9, max_value=40.1,
                              value=39.025, step=0.005, format='%.3f', key='flood_lat')
        lon = st.number_input('Longitude', min_value=-102.1, max_value=-94.5,
                              value=-95.670, step=0.005, format='%.3f', key='flood_lon')
    fraction = {'Dry (20% runoff)': 0.20, 'Typical (45% runoff)': 0.45,
                'Saturated (75% runoff)': 0.75}[soil]
    # Hypothetical volume balance: uniform rain over circular footprint.
    # These volumes do not represent streamflow, peak discharge or flood stage.
    area_sqmi = math.pi * radius ** 2
    volume_acft = rain / 12 * area_sqmi * 640
    runoff_acft = volume_acft * fraction
    a, b, c = st.columns(3)
    a.metric('Average rain intensity', f'{rain / duration:.2f} in/hr')
    b.metric('Hypothetical rainfall volume', f'{volume_acft:,.0f} acre-ft')
    c.metric('Illustrative runoff volume', f'{runoff_acft:,.0f} acre-ft')
    st.caption(f'Calculation assumes uniform {rain:g} inches of rain across a circular '
               f'{area_sqmi:,.1f} square-mile footprint and a fixed {fraction:.0%} runoff '
               'fraction. These assumed fractions are not calibrated to the Shunganunga '
               'watershed. Runoff volume is NOT peak streamflow or inundation depth.')
    if restriction:
        st.info('Restricted-crossing scenario selected. A blockage could increase upstream '
                'water levels, but its effect cannot be quantified without crossing '
                'geometry, flow routing and hydraulic modeling.')
    m = flood_scenario_map(lat, lon, radius, rain, duration, fraction, restriction,
                           basin_geometry=basin_geometry, show_circle=show_circle)
    st_folium(m, width='stretch', height=510, returned_objects=[],
              key='experimental_kansas_flood_map')
    st.caption('Watershed boundary: USGS NLDI/NHDPlusV2 basin above gage 06889630; '
               'not necessarily the full Shunganunga watershed. The 33.5-square-mile '
               'published gage drainage area is a reference, not the area used in the '
               'volume calculation. Volumes still apply only to the hypothetical circle. '
               'OpenStreetMap baselayer. No inundation depths or road overtopping modeled.')
    with st.expander('What is needed before this can estimate flooded roads?'):
        st.markdown("""- **USGS 3DEP terrain:** drainage directions, valley geometry, road elevations.
- **USGS NHD / StreamStats:** actual watershed and channel network.
- **NOAA Atlas 14 / Atlas 15 and rainfall grids:** storm frequency and spatial distribution.
- **USGS streamgages and historic floods:** calibration and validation.
- **Roads, bridges, culverts and construction records:** conveyance and obstructions.
- **HEC-HMS / HEC-RAS or validated HAND-based hydraulics:** runoff routing, stage, and flood depth.

Until these are connected and tested, the prototype intentionally does not color roads as flooded or safe.""")


experimental_flood_panel()


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