#!/usr/bin/env python3
"""Convert American spelling to en-NZ (British) spelling.

Whisper's .en models emit US spelling and there is no en-NZ model, so we apply
a deterministic post-pass to the transcript. We use a CURATED word map rather
than blanket suffix rules on purpose: rules like -ize->-ise or -or->-our
mis-fire on size/prize/seize and doctor/error/mirror. A vetted dictionary only
touches words we know should change.

Matching is case-insensitive and case-preserving (US "Color" -> "Colour",
"COLOR" -> "COLOUR"), and only whole words are replaced (word boundaries), so
substrings inside other words are left alone.
"""
import re

# US -> en-NZ. Keys are lowercase; casing is restored at substitution time.
# Grouped by the rule they illustrate, but applied as a flat lookup.
_WORDS = {
    # -or -> -our
    "color": "colour", "colors": "colours", "colored": "coloured",
    "coloring": "colouring", "colorful": "colourful",
    "honor": "honour", "honors": "honours", "honored": "honoured",
    "honoring": "honouring", "honorable": "honourable",
    "favor": "favour", "favors": "favours", "favored": "favoured",
    "favoring": "favouring", "favorite": "favourite",
    "favorites": "favourites", "favorable": "favourable",
    "labor": "labour", "labors": "labours", "labored": "laboured",
    "neighbor": "neighbour", "neighbors": "neighbours",
    "neighborhood": "neighbourhood", "neighboring": "neighbouring",
    "behavior": "behaviour", "behaviors": "behaviours",
    "behavioral": "behavioural",
    "flavor": "flavour", "flavors": "flavours", "flavored": "flavoured",
    "harbor": "harbour", "harbors": "harbours",
    "humor": "humour", "humored": "humoured", "humorous": "humorous",
    "rumor": "rumour", "rumors": "rumours",
    "vapor": "vapour", "vapors": "vapours",
    "savor": "savour", "savory": "savoury",
    "endeavor": "endeavour", "endeavors": "endeavours",
    "splendor": "splendour", "valor": "valour", "rigor": "rigour",
    "vigor": "vigour", "odor": "odour", "armor": "armour",
    "parlor": "parlour", "tumor": "tumour", "tumors": "tumours",
    # -er -> -re
    "center": "centre", "centers": "centres", "centered": "centred",
    "theater": "theatre", "theaters": "theatres",
    "meter": "metre", "meters": "metres",
    "liter": "litre", "liters": "litres",
    "fiber": "fibre", "fibers": "fibres",
    "caliber": "calibre", "somber": "sombre",
    "specter": "spectre", "luster": "lustre",
    "saber": "sabre", "scepter": "sceptre",
    # -ize/-ization -> -ise/-isation
    "organize": "organise", "organizes": "organises",
    "organized": "organised", "organizing": "organising",
    "organization": "organisation", "organizations": "organisations",
    "realize": "realise", "realizes": "realises",
    "realized": "realised", "realizing": "realising",
    "recognize": "recognise", "recognizes": "recognises",
    "recognized": "recognised", "recognizing": "recognising",
    "apologize": "apologise", "apologized": "apologised",
    "apologizing": "apologising",
    "emphasize": "emphasise", "emphasized": "emphasised",
    "emphasizing": "emphasising",
    "criticize": "criticise", "criticized": "criticised",
    "criticizing": "criticising",
    "prioritize": "prioritise", "prioritized": "prioritised",
    "prioritizing": "prioritising",
    "categorize": "categorise", "categorized": "categorised",
    "categorizing": "categorising",
    "summarize": "summarise", "summarized": "summarised",
    "summarizing": "summarising",
    "minimize": "minimise", "minimized": "minimised",
    "minimizing": "minimising",
    "maximize": "maximise", "maximized": "maximised",
    "maximizing": "maximising",
    "optimize": "optimise", "optimized": "optimised",
    "optimizing": "optimising", "optimization": "optimisation",
    "customize": "customise", "customized": "customised",
    "customizing": "customising",
    "authorize": "authorise", "authorized": "authorised",
    "authorizing": "authorising", "authorization": "authorisation",
    "specialize": "specialise", "specialized": "specialised",
    "specializing": "specialising",
    "standardize": "standardise", "standardized": "standardised",
    "utilize": "utilise", "utilized": "utilised", "utilizing": "utilising",
    "analyze": "analyse", "analyzes": "analyses",
    "analyzed": "analysed", "analyzing": "analysing",
    "paralyze": "paralyse", "paralyzed": "paralysed",
    # -ense -> -ence (nouns)
    "defense": "defence", "defenses": "defences",
    "offense": "offence", "offenses": "offences",
    "pretense": "pretence", "license": "licence",  # noun form
    # -og -> -ogue
    "catalog": "catalogue", "catalogs": "catalogues",
    "dialog": "dialogue", "dialogs": "dialogues",
    "analog": "analogue",
    # doubled-l before suffix
    "traveled": "travelled", "traveling": "travelling",
    "traveler": "traveller", "travelers": "travellers",
    "canceled": "cancelled", "canceling": "cancelling",
    "modeling": "modelling", "modeled": "modelled",
    "labeled": "labelled", "labeling": "labelling",
    "fueled": "fuelled", "fueling": "fuelling",
    "signaled": "signalled", "signaling": "signalling",
    "marveled": "marvelled", "marveling": "marvelling",
    "counseled": "counselled", "counseling": "counselling",
    "totaled": "totalled",
    # misc common
    "gray": "grey", "grayish": "greyish",
    "mold": "mould", "molds": "moulds", "molded": "moulded",
    "plow": "plough", "plows": "ploughs",
    "draft": "draught",  # the airflow/beer sense; ambiguous but NZ-leaning
    "skeptic": "sceptic", "skeptical": "sceptical",
    "skepticism": "scepticism",
    "aluminum": "aluminium",
    "jewelry": "jewellery", "fulfill": "fulfil",
    "enroll": "enrol", "enrollment": "enrolment",
    "installment": "instalment", "skillful": "skilful",
    "willful": "wilful", "practiced": "practised",  # verb
    "practicing": "practising",
    "check": "cheque",  # the payment sense; ambiguous, see note in tests
    "checks": "cheques",
    "tire": "tyre", "tires": "tyres",  # the wheel sense; ambiguous
    "curb": "kerb", "curbs": "kerbs",  # the roadside sense; ambiguous
    "donut": "doughnut", "donuts": "doughnuts",
    "mom": "mum", "moms": "mums",
}

# A few entries above (check, tire, curb, draft) have a US spelling that is a
# legitimate word in en-NZ with a DIFFERENT meaning. Converting them risks
# changing "check the box" -> "cheque the box". We exclude those high-collision
# ones from the default map; they live here as opt-in only.
_AMBIGUOUS = {"check", "checks", "tire", "tires", "curb", "curbs", "draft"}
_SAFE_WORDS = {k: v for k, v in _WORDS.items() if k not in _AMBIGUOUS}

_PATTERN = re.compile(
    r"\b(" + "|".join(sorted(_SAFE_WORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _match_case(src, repl):
    """Apply src's casing to repl: ALL CAPS, Title, or lower."""
    if src.isupper():
        return repl.upper()
    if src[:1].isupper():
        return repl[:1].upper() + repl[1:]
    return repl


def to_nz(text):
    """Return text with American spellings rewritten to en-NZ, case-preserved."""
    def sub(m):
        word = m.group(0)
        return _match_case(word, _SAFE_WORDS[word.lower()])

    return _PATTERN.sub(sub, text)


if __name__ == "__main__":
    import sys
    sys.stdout.write(to_nz(sys.stdin.read()))
