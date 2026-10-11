"""Indian National AQI (CPCB) from particulate concentrations.

CPCB defines the PM sub-indices on 24-hour averages. We apply the same
breakpoints to hourly forecast values so that each school period gets its own
number, so treat the result as an hourly estimate, not the official daily AQI.
"""

# (conc_low, conc_high, index_low, index_high), concentrations in ug/m3
PM25_BREAKPOINTS = [
    (0, 30, 0, 50),
    (30, 60, 50, 100),
    (60, 90, 100, 200),
    (90, 120, 200, 300),
    (120, 250, 300, 400),
    (250, 380, 400, 500),
]
PM10_BREAKPOINTS = [
    (0, 50, 0, 50),
    (50, 100, 50, 100),
    (100, 250, 100, 200),
    (250, 350, 200, 300),
    (350, 430, 300, 400),
    (430, 510, 400, 500),
]

CATEGORIES = [
    (50, "Good"),
    (100, "Satisfactory"),
    (200, "Moderate"),
    (300, "Poor"),
    (400, "Very Poor"),
    (500, "Severe"),
]


def _sub_index(conc, breakpoints):
    if conc is None:
        return None
    conc = max(conc, 0)
    for c_lo, c_hi, i_lo, i_hi in breakpoints:
        if conc <= c_hi:
            return i_lo + (i_hi - i_lo) * (conc - c_lo) / (c_hi - c_lo)
    return 500


def indian_aqi(pm25, pm10):
    """AQI is the worse of the two sub-indices. None if both are missing."""
    subs = [s for s in (_sub_index(pm25, PM25_BREAKPOINTS), _sub_index(pm10, PM10_BREAKPOINTS)) if s is not None]
    if not subs:
        return None
    return min(500, round(max(subs)))


def category(aqi):
    for upper, name in CATEGORIES:
        if aqi <= upper:
            return name
    return "Severe"
