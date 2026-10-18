import html
import requests
import streamlit as st
import folium
from streamlit_folium import st_folium

UA = "KansasNOAAWeatherDashboard/1.0 (replace-with-your-contact-email@example.com)"
HEADERS = {"User-Agent": UA, "Accept": "application/geo+json"}
BOUNDS = [[36.99, -102.05], [40.01, -94.59]]

def get_json(url, params=None, headers=None):
    r = requests.get(url, params=params, headers=headers, timeout=25)
    r.raise_for_status()
    return r.json()

@st.cache_data(ttl=86400)
def zip_location(z):
    data = get_json(f"https://api.zippopotam.us/us/{z}")
    places = data.get("places", [])
    if not places:
        raise ValueError("ZIP code not found.")
    p = places[0]
    if p.get("state", "").lower() != "kansas":
        raise ValueError(f"ZIP {z} resolves to {p.get('state')}, not Kansas.")
    return {"zip": z, "city": p.get("place name", "Unknown"),
            "lat": float(p["latitude"]), "lon": float(p["longitude"])}

@st.cache_data(ttl=1800)
def point_info(lat, lon):
    return get_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", headers=HEADERS)["properties"]

@st.cache_data(ttl=600)
def forecast(url):
    return get_json(url, headers=HEADERS)["properties"]["periods"]

@st.cache_data(ttl=120)
def alerts_for_point(lat, lon):
    return get_json("https://api.weather.gov/alerts/active",
                    {"point": f"{lat:.4f},{lon:.4f}"}, HEADERS).get("features", [])

@st.cache_data(ttl=120)
def kansas_alerts():
    return get_json("https://api.weather.gov/alerts/active",
                    {"area": "KS"}, HEADERS).get("features", [])

def color_for(feature):
    p = feature.get("properties") or {}
    event = (p.get("event") or "").lower()
    if "tornado warning" in event: return "#d7191c"
    if "flash flood warning" in event or "flood warning" in event: return "#762a83"
    if "severe thunderstorm warning" in event: return "#ef8a62"
    severity = (p.get("severity") or "").lower()
    return {"extreme": "#b2182b", "severe": "#ef8a62",
            "moderate": "#fddc6c"}.get(severity, "#4393c3")

def popup(feature):
    p = feature.get("properties") or {}
    fields = [("event", "Alert"), ("severity", "Severity"),
              ("areaDesc", "Affected area"), ("expires", "Expires"),
              ("headline", "Headline"), ("description", "Details"),
              ("instruction", "Instructions")]
    body = ""
    for key, label in fields:
        value = p.get(key)
        if value:
            body += f"<p><b>{label}:</b> {html.escape(str(value))}</p>"
    return folium.Popup(body or "NWS alert details unavailable", max_width=380)

def make_map(features, location):
    m = folium.Map(location=[38.5, -98.0], zoom_start=7,
                   tiles="OpenStreetMap", control_scale=True)
    drawn = missing = 0
    for f in features:
        geom = f.get("geometry")
        if geom and geom.get("type") in ("Polygon", "MultiPolygon"):
            p = f.get("properties") or {}
            folium.GeoJson(
                f, name=p.get("event", "NWS alert"),
                style_function=lambda feat, ff=f: {
                    "color": color_for(ff), "weight": 3,
                    "fillColor": color_for(ff), "fillOpacity": 0.28},
                highlight_function=lambda feat: {"weight": 5, "fillOpacity": 0.45},
                tooltip=(p.get("event", "Weather alert") + " — " +
                         p.get("areaDesc", "Area unavailable")),
                popup=popup(f)
            ).add_to(m)
            drawn += 1
        else:
            missing += 1
    folium.Marker([location["lat"], location["lon"]],
                  tooltip=f'{location["city"]} (ZIP {location["zip"]})',
                  popup=f'{location["city"]}, Kansas {location["zip"]}',
                  icon=folium.Icon(color="blue", icon="home")).add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)
    m.fit_bounds(BOUNDS)
    return m, drawn, missing

def alert_text(f):
    p = f.get("properties") or {}
    st.markdown("### " + p.get("event", "Weather alert"))
    st.write(f'**Severity:** {p.get("severity", "Unknown")} · **Urgency:** {p.get("urgency", "Unknown")}')
    st.caption("Area: " + (p.get("areaDesc") or "Not specified"))
    st.caption("Expires: " + (p.get("ends") or p.get("expires") or "Not specified"))
    if p.get("headline"): st.write(p["headline"])
    if p.get("description"): st.write(p["description"])
    if p.get("instruction"): st.warning(p["instruction"])
    st.divider()

st.set_page_config(page_title="Kansas NOAA Weather Dashboard", page_icon="🌦️", layout="wide")
st.title("🌦️ Kansas NOAA Weather Dashboard")
st.caption("Daily and hourly forecasts plus active NWS alerts and a statewide Kansas alert map.")

with st.sidebar:
    st.header("Location")
    zipcode = st.text_input("Kansas ZIP code", "67202", max_chars=5).strip()
    refresh = st.button("Refresh weather and map", type="primary", use_container_width=True)
    st.caption("Forecast cache: 10 minutes · Alert cache: 2 minutes")
    st.caption("This dashboard is not an emergency notification service.")

if refresh:
    st.cache_data.clear()
if len(zipcode) != 5 or not zipcode.isdigit():
    st.info("Enter a five-digit Kansas ZIP code.")
    st.stop()

try:
    with st.spinner("Loading forecasts and Kansas alerts..."):
        loc = zip_location(zipcode)
        point = point_info(loc["lat"], loc["lon"])
        daily = forecast(point["forecast"])
        hourly = forecast(point["forecastHourly"])
        local_alerts = alerts_for_point(loc["lat"], loc["lon"])
        statewide = kansas_alerts()
except requests.HTTPError as e:
    status = e.response.status_code if e.response is not None else "unknown"
    st.error(f"A weather service returned HTTP {status}. Try again shortly.")
    st.stop()
except requests.RequestException as e:
    st.error(f"Could not connect to a weather service: {e}")
    st.stop()
except (ValueError, KeyError, IndexError, TypeError) as e:
    st.error(str(e))
    st.stop()

st.subheader(f'{loc["city"]}, Kansas {loc["zip"]}')
st.caption(f'Coordinates: {loc["lat"]:.4f}, {loc["lon"]:.4f} · NWS office: {point.get("gridId", "Unknown")}')

st.subheader("Kansas active alert map")
st.write("Click a shaded alert area for details. The blue marker is your selected ZIP-code location.")
m, drawn, missing = make_map(statewide, loc)
st_folium(m, use_container_width=True, height=600, returned_objects=[])
a, b, c = st.columns(3)
a.metric("Active Kansas alerts", len(statewide))
b.metric("Polygons mapped", drawn)
c.metric("Alerts without geometry", missing)
if missing:
    st.info(f"{missing} alert(s) did not include map geometry in the NWS response and cannot be drawn as exact polygons. Their text details are listed below.")

with st.expander("All active Kansas alerts", expanded=bool(statewide)):
    if not statewide: st.success("No active Kansas alerts were returned.")
    for f in statewide: alert_text(f)

st.subheader("Alerts for selected ZIP code")
if not local_alerts: st.success("No active alerts were returned for this point.")
else:
    for f in local_alerts: alert_text(f)

st.subheader("Daily forecast")
cols = st.columns(2)
for i, p in enumerate(daily):
    with cols[i % 2]:
        with st.container(border=True):
            st.markdown(f'**{p.get("name", "Forecast period")}**')
            st.metric("Temperature", f'{p.get("temperature", "—")}°{p.get("temperatureUnit", "F")}')
            st.write("**Conditions:** " + p.get("shortForecast", "Unavailable"))
            st.write(f'**Wind:** {p.get("windSpeed", "—")} {p.get("windDirection", "")}')
            pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
            st.write("**Precipitation chance:** " + (f"{pop}%" if pop is not None else "Not provided"))
            if p.get("detailedForecast"): st.write(p["detailedForecast"])

st.subheader("Hourly forecast")
count = st.slider("Hours to display", 6, 48, 24, 6)
rows = []
for p in hourly[:count]:
    pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
    hum = (p.get("relativeHumidity") or {}).get("value")
    rows.append({
        "Time": p.get("startTime", "")[:16].replace("T", " "),
        "Temperature": f'{p.get("temperature", "—")}°{p.get("temperatureUnit", "F")}',
        "Conditions": p.get("shortForecast", "Unavailable"),
        "Precip. chance": f"{pop}%" if pop is not None else "—",
        "Wind": f'{p.get("windSpeed", "—")} {p.get("windDirection", "")}',
        "Humidity": f"{hum}%" if hum is not None else "—",
    })
st.dataframe(rows, use_container_width=True, hide_index=True)

st.caption("Sources: api.weather.gov and Zippopotam.us. NWS may omit alert geometry; not every alert can appear as a polygon. Always verify official warnings.")
st.markdown("[NWS API documentation](https://www.weather.gov/documentation/services-web-api) · [All active Kansas alerts](https://api.weather.gov/alerts/active?area=KS)")
