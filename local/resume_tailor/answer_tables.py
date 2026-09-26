"""Static tables for the answer store (`apply_answers`): the option lists of the
built-in choice answers, the US states, a bundled country list, and the alias
table a version 1 answer is matched through when it migrates.

Pure data with no imports, so `apply_judge` reads `US_STATES` from here without
pulling in the rest of the package: both sides match against one list.
"""

# Two-letter code -> full name: the 50 states plus the District of Columbia.
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia"}

# The address_state options: the full names, A to Z.
STATE_NAMES = tuple(sorted(US_STATES.values()))

US_COUNTRY = "United States"

# The address_country options: country short names, A to Z.
COUNTRIES = (
    "Afghanistan", "Albania", "Algeria", "Andorra", "Angola", "Antigua and Barbuda",
    "Argentina", "Armenia", "Australia", "Austria", "Azerbaijan", "Bahamas", "Bahrain",
    "Bangladesh", "Barbados", "Belarus", "Belgium", "Belize", "Benin", "Bhutan", "Bolivia",
    "Bosnia and Herzegovina", "Botswana", "Brazil", "Brunei", "Bulgaria", "Burkina Faso",
    "Burundi", "Cabo Verde", "Cambodia", "Cameroon", "Canada", "Central African Republic",
    "Chad", "Chile", "China", "Colombia", "Comoros", "Congo", "Costa Rica",
    "C\u00f4te d'Ivoire", "Croatia", "Cuba", "Cyprus", "Czechia",
    "Democratic Republic of the Congo", "Denmark", "Djibouti", "Dominica",
    "Dominican Republic", "Ecuador", "Egypt", "El Salvador", "Equatorial Guinea", "Eritrea",
    "Estonia", "Eswatini", "Ethiopia", "Fiji", "Finland", "France", "Gabon", "Gambia",
    "Georgia", "Germany", "Ghana", "Greece", "Grenada", "Guatemala", "Guinea",
    "Guinea-Bissau", "Guyana", "Haiti", "Honduras", "Hong Kong", "Hungary", "Iceland",
    "India", "Indonesia", "Iran", "Iraq", "Ireland", "Israel", "Italy", "Jamaica", "Japan",
    "Jordan", "Kazakhstan", "Kenya", "Kiribati", "Kosovo", "Kuwait", "Kyrgyzstan", "Laos",
    "Latvia", "Lebanon", "Lesotho", "Liberia", "Libya", "Liechtenstein", "Lithuania",
    "Luxembourg", "Madagascar", "Malawi", "Malaysia", "Maldives", "Mali", "Malta",
    "Marshall Islands", "Mauritania", "Mauritius", "Mexico", "Micronesia", "Moldova",
    "Monaco", "Mongolia", "Montenegro", "Morocco", "Mozambique", "Myanmar", "Namibia",
    "Nauru", "Nepal", "Netherlands", "New Zealand", "Nicaragua", "Niger", "Nigeria",
    "North Korea", "North Macedonia", "Norway", "Oman", "Pakistan", "Palau", "Palestine",
    "Panama", "Papua New Guinea", "Paraguay", "Peru", "Philippines", "Poland", "Portugal",
    "Puerto Rico", "Qatar", "Romania", "Russia", "Rwanda", "Saint Kitts and Nevis",
    "Saint Lucia", "Saint Vincent and the Grenadines", "Samoa", "San Marino",
    "Sao Tome and Principe", "Saudi Arabia", "Senegal", "Serbia", "Seychelles",
    "Sierra Leone", "Singapore", "Slovakia", "Slovenia", "Solomon Islands", "Somalia",
    "South Africa", "South Korea", "South Sudan", "Spain", "Sri Lanka", "Sudan", "Suriname",
    "Sweden", "Switzerland", "Syria", "Taiwan", "Tajikistan", "Tanzania", "Thailand",
    "Timor-Leste", "Togo", "Tonga", "Trinidad and Tobago", "Tunisia", "Turkey",
    "Turkmenistan", "Tuvalu", "Uganda", "Ukraine", "United Arab Emirates", "United Kingdom",
    "United States", "Uruguay", "Uzbekistan", "Vanuatu", "Vatican City", "Venezuela",
    "Vietnam", "Yemen", "Zambia", "Zimbabwe",
)

DECLINE = "Decline to self-identify"

GENDER_OPTIONS = ("Male", "Female", "Non-binary", DECLINE)

RACE_OPTIONS = (
    "American Indian or Alaska Native", "Asian", "Black or African American",
    "Hispanic or Latino", "Native Hawaiian or Other Pacific Islander", "White",
    "Two or more races", DECLINE)

VETERAN_OPTIONS = (
    "I am not a protected veteran",
    "I identify as one or more of the classifications of protected veteran",
    DECLINE)

DISABILITY_OPTIONS = (
    "Yes, I have a disability, or have had one in the past",
    "No, I do not have a disability and have not had one in the past",
    DECLINE)

# Another way to write a choice answer -> the option it means, keyed by the
# built-in id it applies to (a v1 country "GA" must not become the state
# Georgia, and "No" means something different for veteran_status than for
# disability_status). A migrating answer is matched against its own question's
# options first (case aside), then through its id's entry here; an alias counts
# only when its option is one of that question's options.
_EEO_DECLINE_ALIASES = {
    "Decline": DECLINE,
    "Prefer not to say": DECLINE,
    "I don't wish to answer": DECLINE,
}

CHOICE_ALIASES = {
    "gender": {
        **_EEO_DECLINE_ALIASES,
        "Man": "Male", "M": "Male",
        "Woman": "Female", "F": "Female",
    },
    "race_ethnicity": {
        **_EEO_DECLINE_ALIASES,
        "Black": "Black or African American",
        "African American": "Black or African American",
        "Hispanic": "Hispanic or Latino",
        "Latino": "Hispanic or Latino",
        "Latina": "Hispanic or Latino",
        "Latinx": "Hispanic or Latino",
        "Native American": "American Indian or Alaska Native",
        "Pacific Islander": "Native Hawaiian or Other Pacific Islander",
        "Caucasian": "White",
        "Multiracial": "Two or more races",
        "Mixed": "Two or more races",
        "Two or more": "Two or more races",
    },
    "veteran_status": {
        **_EEO_DECLINE_ALIASES,
        "I am not a veteran": "I am not a protected veteran",
        "No": "I am not a protected veteran",
        "Not a veteran": "I am not a protected veteran",
        "Non-veteran": "I am not a protected veteran",
        "Yes": "I identify as one or more of the classifications of protected veteran",
        "Protected veteran":
            "I identify as one or more of the classifications of protected veteran",
    },
    "disability_status": {
        **_EEO_DECLINE_ALIASES,
        "No": "No, I do not have a disability and have not had one in the past",
        "No disability": "No, I do not have a disability and have not had one in the past",
        "I do not have a disability":
            "No, I do not have a disability and have not had one in the past",
        "Yes": "Yes, I have a disability, or have had one in the past",
        "I have a disability": "Yes, I have a disability, or have had one in the past",
    },
    "address_country": {
        "US": US_COUNTRY,
        "USA": US_COUNTRY,
        "U.S.": US_COUNTRY,
        "U.S.A.": US_COUNTRY,
        "United States of America": US_COUNTRY,
    },
    "address_state": dict(US_STATES),
}
