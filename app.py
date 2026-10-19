import html
from datetime import datetime, timezone

import folium
import requests
import streamlit as st
from streamlit_folium import st_folium

st.set_page_config(page_title='FAweather | Kansas Weather Alerts', page_icon='🌪️', layout='wide')

# Replace with an email address you control before publishing.
USER_AGENT = 'FAweather/2.0 (contact: replace-with-your-email@example.com)'
HEADERS = {'User-Agent': USER_AGENT, 'Accept': 'application/geo+json'}
KS_BOUNDS = [[36.99, -102.05], [40.01, -94.59]]

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
    if place.get('state', '').lower() != 'kansas':
        raise ValueError('Please enter a Kansas ZIP code.')
    return {'city': place['place name'], 'zip': code,
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
def latest_radar_frame():
    """RainViewer public radar manifest; only past observed frames, not forecasts."""
    data = requests.get('https://api.rainviewer.com/public/weather-maps.json', timeout=15).json()
    frames = (data.get('radar') or {}).get('past') or []
    if not frames:
        raise ValueError('No radar frames currently available')
    frame = frames[-1]
    host = data.get('host', '')
    path = frame.get('path', '')
    if not host.startswith('https://') or not path.startswith('/v2/radar/'):
        raise ValueError('Unexpected radar service response')
    return host + path + '/256/{z}/{x}/{y}/2/1_1.png', frame['time']


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
    st.caption(f"{p.get('areaDesc') or 'Area not specified'} | Ends: {p.get('ends') or p.get('expires') or 'Not specified'}")
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


def make_warning_map(alerts, loc, radar_tiles=None, radar_opacity=0.7):
    m = folium.Map(location=[38.5, -98], zoom_start=7, tiles='OpenStreetMap', control_scale=True)
    # Radar is drawn first so NWS warning outlines remain above it.
    if radar_tiles:
        folium.TileLayer(
            tiles=radar_tiles, name='Precipitation radar (RainViewer)',
            attr='Radar: RainViewer (rainviewer.com)', overlay=True,
            control=True, show=True, opacity=radar_opacity,
            max_native_zoom=7, max_zoom=18,
        ).add_to(m)
    drawn = 0
    for feature in alerts:
        geom = feature.get('geometry')
        if not geom or geom.get('type') not in ('Polygon', 'MultiPolygon'):
            continue
        p = feature.get('properties') or {}
        label, color = category(feature)
        title = html.escape(p.get('event') or label)
        area = html.escape(p.get('areaDesc') or 'Area not specified')
        ends = html.escape(str(p.get('ends') or p.get('expires') or 'Unknown'))
        tag = elevated_tag(feature)
        special = f'<p><strong>{html.escape(tag)}</strong></p>' if tag else ''
        popup_html = f'<div style="width:300px"><h4>{title}</h4>{special}<p>{area}</p><p>Ends: {ends}</p></div>'
        folium.GeoJson(
            geom,
            name=label,
            style_function=lambda _, c=color: {'color': c, 'weight': 3, 'fillColor': c, 'fillOpacity': 0.30},
            highlight_function=lambda _: {'weight': 5, 'fillOpacity': 0.48},
            tooltip=f"{p.get('event') or label} — {p.get('areaDesc') or 'Kansas'}",
            popup=folium.Popup(popup_html, max_width=340),
        ).add_to(m)
        drawn += 1
    folium.Marker([loc['lat'], loc['lon']], tooltip=f"{loc['city']} ({loc['zip']})",
                  icon=folium.Icon(color='blue', icon='home')).add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    m.fit_bounds(KS_BOUNDS)
    return m, drawn


st.title('🌪️ FAweather | Kansas Severe Weather Center')
st.caption('Official NWS alerts • Radar + warning polygons on one map • Local forecasts')
st.markdown('<div class="weather-note">⚠️ This site is supplemental weather information, not an emergency notification system. Keep Wireless Emergency Alerts and NOAA Weather Radio enabled. Always follow official NWS instructions.</div>', unsafe_allow_html=True)

with st.sidebar:
    st.header('Your Kansas location')
    zipcode = st.text_input('ZIP code', '67202', max_chars=5).strip()
    refresh = st.button('Refresh all data', type='primary', width='stretch')
    st.caption('Alerts automatically refresh about every 2 minutes while this page is open. Forecasts refresh every 10 minutes.')
    st.markdown('[Official NWS Kansas alerts](https://api.weather.gov/alerts/active?area=KS)')
    st.markdown('[NWS radar viewer](https://radar.weather.gov/)')

if refresh:
    st.cache_data.clear()
if len(zipcode) != 5 or not zipcode.isdigit():
    st.info('Enter a valid five-digit Kansas ZIP code.')
    st.stop()

try:
    loc = lookup_zip(zipcode)
    point = nws_point(loc['lat'], loc['lon'])
except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
    st.error(f'Could not load your Kansas location: {exc}')
    st.stop()

st.subheader(f"📍 {loc['city']}, Kansas {loc['zip']}")
st.caption(f"NWS forecast office: {point.get('gridId', 'Unknown')}")


@st.fragment(run_every='2min')
def live_warnings():
    st.subheader('Live Kansas weather warnings')
    try:
        statewide, fetched_at = active_alerts(area='KS')
        local, _ = active_alerts(point=f"{loc['lat']:.4f},{loc['lon']:.4f}")
    except requests.RequestException as exc:
        st.error(f'NWS alerts could not be refreshed: {exc}. Do not assume there are no active warnings.')
        st.markdown('[Check the official NWS alerts](https://www.weather.gov/)')
        return

    fetched = datetime.fromisoformat(fetched_at).astimezone().strftime('%b %d, %Y at %I:%M:%S %p %Z')
    st.caption(f'Last successful NWS alert fetch: {fetched} • Automatic check approximately every 2 minutes while open')
    counts = {key: sum(category(a)[0] == key for a in statewide) for key in
              ('Tornado warning', 'Severe thunderstorm warning', 'Flash flood warning')}
    cols = st.columns(4)
    cols[0].metric('All active KS alerts', len(statewide))
    cols[1].metric('Tornado warnings', counts['Tornado warning'])
    cols[2].metric('Severe T-storm warnings', counts['Severe thunderstorm warning'])
    cols[3].metric('Flash flood warnings', counts['Flash flood warning'])

    priority = [a for a in statewide if elevated_tag(a)]
    if priority:
        st.error(f'⚠️ {len(priority)} high-priority alert(s) detected. Review the alert details immediately.')
        for a in priority:
            st.write(f"**{elevated_tag(a)}:** {(a.get('properties') or {}).get('areaDesc') or 'See alert details'}")

    st.markdown('**Radar + NWS warning map** — radar precipitation beneath clickable official warning polygons.')
    radar_tiles = None
    radar_time = None
    try:
        radar_tiles, radar_time = latest_radar_frame()
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        st.warning(f'Radar imagery is temporarily unavailable ({exc}). NWS warnings are still displayed.')
    if radar_time:
        radar_utc = datetime.fromtimestamp(radar_time, tz=timezone.utc).strftime('%b %d, %Y %H:%M UTC')
        st.caption(f'Radar frame timestamp: {radar_utc} (radar can be delayed). Use map layer control to toggle radar.')
    m, drawn = make_warning_map(statewide, loc, radar_tiles)

    st_folium(m, width='stretch', height=540, returned_objects=[], key='ks_warning_map')
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

    with st.expander(f'All Kansas alerts ({len(statewide)})', expanded=False):
        if not statewide:
            st.info('NWS returned no active Kansas alerts at the last successful check.')
        for a in sorted(statewide, key=lambda f: 0 if 'warning' in ((f.get('properties') or {}).get('event') or '').lower() else 1):
            show_alert(a)


live_warnings()

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
            rows.append({'Time': p.get('startTime', '')[:16].replace('T', ' '),
                         'Temp': f"{p.get('temperature', '—')}°{p.get('temperatureUnit', 'F')}",
                         'Conditions': p.get('shortForecast', '—'),
                         'Rain chance': f'{pop}%' if pop is not None else '—',
                         'Wind': f"{p.get('windSpeed', '—')} {p.get('windDirection', '')}"})
        st.dataframe(rows, width='stretch', hide_index=True)

st.divider()
st.markdown('[Radar imagery by RainViewer](https://www.rainviewer.com/) · [Official NWS radar](https://radar.weather.gov/)')
st.caption('Sources: U.S. National Weather Service API (weather.gov), RainViewer radar (rainviewer.com), Zippopotam.us, OpenStreetMap. Data and network availability may be delayed. No guarantees of continuous monitoring or alert delivery. Not affiliated with NOAA or the NWS.')
