"""
core/mcp_servers/weather_server.py
──────────────────────────────────
High-precision Global Weather MCP Server for JARVIS powered by Open-Meteo.
Zero API keys required. Worldwide support (Pretoria, Johannesburg, London, Tokyo, etc.).

Tools:
  - get_current_weather(location: str)
  - get_weather_forecast(location: str, days: int = 5)
  - get_hourly_forecast(location: str, hours: int = 12)
  - get_air_quality(location: str)
"""

import asyncio
import sys
from typing import Optional, Tuple
import requests
from mcp.server.mcpserver import MCPServer

server = MCPServer("WeatherServer")

# WMO Weather interpretation codes (WW)
_WMO_CODE_MAP = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    71: "Slight snow fall",
    73: "Moderate snow fall",
    75: "Heavy snow fall",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def _interpret_wmo(code: Optional[int]) -> str:
    if code is None:
        return "Unknown"
    return _WMO_CODE_MAP.get(code, f"Weather Code {code}")


def _geocode(location: str) -> Tuple[float, float, str]:
    """Resolve location string to (latitude, longitude, full_name)."""
    resp = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": location.strip(), "count": 1, "language": "en", "format": "json"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results")
    if not results:
        raise ValueError(f"Could not find coordinates for location: '{location}'")
    first = results[0]
    lat = first["latitude"]
    lon = first["longitude"]
    name = first.get("name", location)
    admin = first.get("admin1", "")
    country = first.get("country", "")
    full_name = ", ".join(p for p in [name, admin, country] if p)
    return lat, lon, full_name


@server.tool()
def get_current_weather(location: str) -> str:
    """Get real-time current weather conditions for any city or location in the world."""
    try:
        lat, lon, place = _geocode(location)
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,surface_pressure",
                "timezone": "auto",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        curr = data.get("current", {})

        temp = curr.get("temperature_2m", "N/A")
        feels_like = curr.get("apparent_temperature", "N/A")
        humidity = curr.get("relative_humidity_2m", "N/A")
        wind = curr.get("wind_speed_10m", "N/A")
        rain = curr.get("precipitation", 0.0)
        code = curr.get("weather_code", 0)
        condition = _interpret_wmo(code)

        return (
            f"Weather for {place}:\n"
            f"- Condition: {condition}\n"
            f"- Temperature: {temp} C (Feels like: {feels_like} C)\n"
            f"- Humidity: {humidity}%\n"
            f"- Wind Speed: {wind} km/h\n"
            f"- Precipitation: {rain} mm"
        )
    except Exception as e:
        return f"Error retrieving current weather for '{location}': {e}"


@server.tool()
def get_weather_forecast(location: str, days: int = 5) -> str:
    """Get multi-day daily weather forecast (highs, lows, rain probability, conditions) for any location (up to 7 days)."""
    try:
        days = max(1, min(days, 7))
        lat, lon, place = _geocode(location)
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,uv_index_max,sunrise,sunset",
                "timezone": "auto",
                "forecast_days": days,
            },
            timeout=10,
        )
        resp.raise_for_status()
        daily = resp.json().get("daily", {})

        dates = daily.get("time", [])
        max_temps = daily.get("temperature_2m_max", [])
        min_temps = daily.get("temperature_2m_min", [])
        precip_probs = daily.get("precipitation_probability_max", [])
        codes = daily.get("weather_code", [])
        uvs = daily.get("uv_index_max", [])

        lines = [f"{days}-Day Weather Forecast for {place}:\n"]
        for i in range(len(dates)):
            cond = _interpret_wmo(codes[i] if i < len(codes) else None)
            hi = max_temps[i] if i < len(max_temps) else "N/A"
            lo = min_temps[i] if i < len(min_temps) else "N/A"
            pop = precip_probs[i] if i < len(precip_probs) else 0
            uv = uvs[i] if i < len(uvs) else "N/A"
            lines.append(
                f"- {dates[i]}: {cond} | High: {hi} C, Low: {lo} C | Rain Chance: {pop}% | Max UV: {uv}"
            )

        return "\n".join(lines)
    except Exception as e:
        return f"Error retrieving forecast for '{location}': {e}"


@server.tool()
def get_hourly_forecast(location: str, hours: int = 12) -> str:
    """Get hourly weather forecast (temperature, rain probability, wind) for the next N hours (up to 24 hours)."""
    try:
        hours = max(1, min(hours, 24))
        lat, lon, place = _geocode(location)
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "temperature_2m,precipitation_probability,weather_code,wind_speed_10m",
                "timezone": "auto",
                "forecast_hours": hours,
            },
            timeout=10,
        )
        resp.raise_for_status()
        hourly = resp.json().get("hourly", {})

        times = hourly.get("time", [])[:hours]
        temps = hourly.get("temperature_2m", [])[:hours]
        pops = hourly.get("precipitation_probability", [])[:hours]
        codes = hourly.get("weather_code", [])[:hours]

        lines = [f"Hourly Forecast for {place} (Next {len(times)} Hours):\n"]
        for i in range(len(times)):
            t_str = times[i].split("T")[-1]
            cond = _interpret_wmo(codes[i] if i < len(codes) else None)
            temp = temps[i] if i < len(temps) else "N/A"
            pop = pops[i] if i < len(pops) else 0
            lines.append(f"- {t_str}: {temp} C, Rain: {pop}% ({cond})")

        return "\n".join(lines)
    except Exception as e:
        return f"Error retrieving hourly forecast for '{location}': {e}"


@server.tool()
def get_air_quality(location: str) -> str:
    """Get current air quality data (European AQI, US AQI, PM2.5, PM10, Ozone, Nitrogen Dioxide) for any location."""
    try:
        lat, lon, place = _geocode(location)
        resp = requests.get(
            "https://air-quality-api.open-meteo.com/v1/air-quality",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "european_aqi,us_aqi,pm10,pm2_5,nitrogen_dioxide,ozone",
                "timezone": "auto",
            },
            timeout=10,
        )
        resp.raise_for_status()
        curr = resp.json().get("current", {})

        us_aqi = curr.get("us_aqi", "N/A")
        eu_aqi = curr.get("european_aqi", "N/A")
        pm25 = curr.get("pm2_5", "N/A")
        pm10 = curr.get("pm10", "N/A")
        no2 = curr.get("nitrogen_dioxide", "N/A")
        o3 = curr.get("ozone", "N/A")

        # Basic AQI rating descriptor
        rating = "Good"
        if isinstance(us_aqi, (int, float)):
            if us_aqi > 150:
                rating = "Unhealthy"
            elif us_aqi > 100:
                rating = "Unhealthy for Sensitive Groups"
            elif us_aqi > 50:
                rating = "Moderate"
            else:
                rating = "Good"

        return (
            f"Air Quality for {place}:\n"
            f"- Rating: {rating} (US AQI: {us_aqi}, EU AQI: {eu_aqi})\n"
            f"- PM2.5: {pm25} ug/m3\n"
            f"- PM10: {pm10} ug/m3\n"
            f"- Nitrogen Dioxide (NO2): {no2} ug/m3\n"
            f"- Ozone (O3): {o3} ug/m3"
        )
    except Exception as e:
        return f"Error retrieving air quality for '{location}': {e}"



if __name__ == "__main__":
    asyncio.run(server.run_stdio_async())
