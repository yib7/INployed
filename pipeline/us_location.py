"""Is a LinkedIn `job_location` outside the United States?

The scrape asks Bright Data for `location: United States, country: US`, but some
runs hand back whole inputs with a worldwide mix (Jakarta, London, Singapore,
Bengaluru), and the scorer's prompts are told location was already vetted. This
is the check that makes that true: `score_jobs.add_filter_columns` turns it into
`filter_non_us`, and the dashboard's High Score view hides the same rows for
anything scored before the filter existed.

Pure stdlib with no repo imports, so it runs beside `score_jobs.py` on the VM
and `local/jobsdata.py` loads it through the pipeline/ sys.path hop.

LinkedIn writes a location as "City, ST", "Region, United States", "United
States", "City, State Metropolitan Area", a bare US metro ("Greater Boston",
"Dallas-Fort Worth Metroplex"), or for a foreign job "City, Region, Country",
a bare country ("Brazil") or a foreign metro ("Greater Bengaluru Area"). The
rule leans toward keeping a job: only a positive sign of another country drops
it, and a blank or unrecognised single name stays.
"""
from __future__ import annotations

import re
import unicodedata

# 50 states, DC and the inhabited territories (US jobs, no sponsorship question).
_US_CODES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE "
    "NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR GU VI "
    "AS MP".split())
_US_NAMES = frozenset((
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina",
    "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming", "district of columbia",
    "washington dc", "washington d.c.", "d.c.", "puerto rico", "guam", "u.s. virgin islands",
    "us virgin islands", "american samoa", "northern mariana islands",
    "united states", "united states of america", "usa", "u.s.", "u.s.a.", "us",
))

# Countries and regions that mark a job as abroad, accents stripped and lower
# case. Georgia is left out: a bare "Georgia" is far likelier the state.
_FOREIGN_COUNTRIES = frozenset((
    "afghanistan", "albania", "algeria", "andorra", "angola", "antigua and barbuda",
    "argentina", "armenia", "australia", "austria", "azerbaijan", "bahamas", "bahrain",
    "bangladesh", "barbados", "belarus", "belgium", "belize", "benin", "bhutan", "bolivia",
    "bosnia and herzegovina", "botswana", "brazil", "brunei", "bulgaria", "burkina faso",
    "burundi", "cabo verde", "cambodia", "cameroon", "canada", "central african republic",
    "chad", "chile", "china", "colombia", "comoros", "congo", "costa rica",
    "cote d'ivoire", "croatia", "cuba", "cyprus", "czechia", "czech republic",
    "democratic republic of the congo", "denmark", "djibouti", "dominica",
    "dominican republic", "ecuador", "egypt", "el salvador", "equatorial guinea",
    "eritrea", "estonia", "eswatini", "ethiopia", "fiji", "finland", "france", "gabon",
    "gambia", "germany", "ghana", "greece", "grenada", "guatemala", "guinea",
    "guinea-bissau", "guyana", "haiti", "honduras", "hong kong", "hong kong sar",
    "hungary", "iceland", "india", "indonesia", "iran", "iraq", "ireland", "israel",
    "italy", "jamaica", "japan", "jordan", "kazakhstan", "kenya", "kiribati", "kosovo",
    "kuwait", "kyrgyzstan", "laos", "latvia", "lebanon", "lesotho", "liberia", "libya",
    "liechtenstein", "lithuania", "luxembourg", "macao sar", "macau", "madagascar",
    "malawi", "malaysia", "maldives", "mali", "malta", "marshall islands", "mauritania",
    "mauritius", "mexico", "micronesia", "moldova", "monaco", "mongolia", "montenegro",
    "morocco", "mozambique", "myanmar", "namibia", "nauru", "nepal", "netherlands",
    "the netherlands", "new zealand", "nicaragua", "niger", "nigeria", "north korea",
    "north macedonia", "norway", "oman", "pakistan", "palau", "palestine", "panama",
    "papua new guinea", "paraguay", "peru", "philippines", "poland", "portugal", "qatar",
    "romania", "russia", "russian federation", "rwanda", "saint kitts and nevis",
    "saint lucia", "saint vincent and the grenadines", "samoa", "san marino",
    "sao tome and principe", "saudi arabia", "senegal", "serbia", "seychelles",
    "sierra leone", "singapore", "slovakia", "slovenia", "solomon islands", "somalia",
    "south africa", "south korea", "korea", "republic of korea", "south sudan", "spain",
    "sri lanka", "sudan", "suriname", "sweden", "switzerland", "syria", "taiwan",
    "tajikistan", "tanzania", "thailand", "timor-leste", "togo", "tonga",
    "trinidad and tobago", "tunisia", "turkey", "turkiye", "turkmenistan", "tuvalu",
    "uganda", "ukraine", "united arab emirates", "uae", "united kingdom", "uk",
    "great britain", "england", "scotland", "wales", "northern ireland", "uruguay",
    "uzbekistan", "vanuatu", "vatican city", "venezuela", "vietnam", "viet nam", "yemen",
    "zambia", "zimbabwe", "european union", "european economic area", "europe", "emea",
    "apac", "asia pacific", "latin america", "middle east",
))

# Foreign cities LinkedIn names in a bare metro ("Greater Bengaluru Area",
# "Hanoi Capital Region"). Cities that share a name with a US place (London,
# Paris, Dublin, Rome, Melbourne, Vienna, Berlin, Milan, Lima, Vancouver) are
# left out: "Utica-Rome Area" is in New York, "Portland-Vancouver Area" in
# Oregon and Washington.
_FOREIGN_CITIES = (
    "toronto", "montreal", "calgary", "ottawa", "edmonton", "winnipeg",
    "mexico city", "guadalajara", "monterrey", "sao paulo", "rio de janeiro",
    "buenos aires", "bogota", "medellin", "santiago de chile",
    "greater london", "greater paris", "ile-de-france", "madrid", "barcelona",
    "lisbon", "porto", "amsterdam", "rotterdam", "eindhoven", "the hague", "brussels",
    "antwerp", "louvain", "leuven", "copenhagen", "stockholm", "oslo", "helsinki",
    "munich", "frankfurt", "hamburg", "cologne", "stuttgart", "zurich", "geneva",
    "warsaw", "cracow", "krakow", "wroclaw", "gdansk", "prague", "budapest", "bucharest",
    "sofia", "istanbul", "tel aviv", "dubai", "abu dhabi", "riyadh", "doha", "cairo",
    "lagos", "nairobi", "johannesburg", "cape town", "mumbai", "delhi", "new delhi",
    "noida", "gurgaon", "gurugram", "bengaluru", "bangalore", "hyderabad", "chennai",
    "kolkata", "pune", "ahmedabad", "metro manila", "cebu", "jakarta", "kuala lumpur",
    "bangkok", "hanoi", "ho chi minh", "tokyo", "osaka", "seoul", "beijing", "shanghai",
    "shenzhen", "guangzhou", "taipei", "sydney", "brisbane", "auckland",
)
_FOREIGN_CITY_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(c) for c in sorted(_FOREIGN_CITIES, key=len, reverse=True))
    + r")\b")

# "Austin, Texas Metropolitan Area" / "Greater Birmingham, Alabama Area": the
# state name sits in front of the area word.
_AREA_SUFFIX_RE = re.compile(r"\s+(?:metropolitan\s+)?(?:area|region|metroplex|metro)$")
_SPACES_RE = re.compile(r"\s+")
# Text after the state that says nothing about the country: "New York, NY
# (Hybrid)", "Chicago, IL 60601".
_TRAILER_RE = re.compile(r"\s*\([^)]*\)\s*$|\s+\d{5}(?:-\d{4})?$")


def _fold(text: str) -> str:
    """Lower case, accents stripped, spaces collapsed ("Türkiye" -> "turkiye")."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _SPACES_RE.sub(" ", text).strip().lower()


def _is_us_code(text: str) -> bool:
    """A state code, or several joined by hyphens ("MO-KS", "DC-MD-VA")."""
    codes = text.upper().split("-")
    return all(len(c) == 2 and c in _US_CODES for c in codes)


def _is_us_part(part: str) -> bool:
    part = _TRAILER_RE.sub("", part).strip()
    folded = _fold(part)
    bare = _AREA_SUFFIX_RE.sub("", folded)
    return (_is_us_code(part) or _is_us_code(bare)
            or folded in _US_NAMES or bare in _US_NAMES)


def is_non_us_location(location) -> bool:
    """True when `location` names a place outside the United States.

    A comma-separated location is foreign unless its last part is a US state
    (code or name, with or without "Metropolitan Area") or the United States:
    LinkedIn ends every US one that way and every foreign one with a country or
    a non-US region ("Toronto, ON"). A single name is foreign only when it is a
    known country or a known foreign city; blank and anything unrecognised
    (most bare US metros) count as US so a real US job is never dropped.
    """
    if location is None or isinstance(location, float):  # None / NaN
        return False
    text = str(location).strip()
    if not text:
        return False
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return False
    if _is_us_part(parts[-1]):
        return False
    if len(parts) > 1:
        return True
    folded = _fold(parts[0])
    bare = _AREA_SUFFIX_RE.sub("", folded)
    return (folded in _FOREIGN_COUNTRIES or bare in _FOREIGN_COUNTRIES
            or _FOREIGN_CITY_RE.search(folded) is not None)
