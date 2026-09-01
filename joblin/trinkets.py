"""Trinkets — the end-of-month *objet d'art* generator and the vitrine.

The deal
--------
At the close of each month, a worker earns one inert, decorative **trinket** for
*every whole multiple* of the guild's **bar** (``/joblinconfig item_bar``, default
:data:`DEFAULT_BAR`) their monthly puntos reached — clear it once for one, twice
over (50 puntos against a 25-punto bar) for two. A month is judged by the bar in
force when it *closed* (``bot.bar_for``): re-barring the guild later never redraws
a finished month. Each trinket draws its own
**zone**: the month's featured ("in-season") zone is favoured (~70%), but the odd
one strays in from another — a rotating *bonus*, not a monopoly. A trinket has no
mechanical effect and costs no puntos; it is a milestone reward sitting *beside*
the ⭐ star, not a purchase. The chore economy stays sealed: puntos are never
spent, so none are ever created from nothing.

Why it's all derived, never stored
-----------------------------------
A star is *derived* from the completion log every time the leaderboard is drawn
(see ``bot.star_counts``). A trinket is a *random roll*, so naive re-derivation
would reroll it on every view. We fix that by making both rolls **deterministic**:
the trinket's zone is drawn from ``sha256("zone-pick", guild, user, year-month,
idx)`` and then the item from ``sha256("trinket", guild, user, year-month, zone,
idx)`` — facts already pinned in the ledger — so :func:`roll_for` returns the
*same* trinket every single time, on every machine, across restarts. That buys us
the star's elegance with none of its hazards: no month-close job, no persisted
award state, no double-award races. A whole vitrine is a pure function of
``(completion log, bar history, the zone schedule)``.

Two warnings live in the seeding:

* We use :mod:`hashlib`, **not** the builtin ``hash()`` — the latter is salted
  per process (``PYTHONHASHSEED``) and would hand back a different trinket after
  every reboot.
* ``random.Random(int)`` is the only RNG used. Its stream is stable across
  CPython versions for a given seed, which is what makes the collection durable.

The shape of a roll
-------------------
Every itemy table in our two source corpora — *Vaults of Vaarn* and ctrlcreep's
*Flayed Sun* — decomposes into a **genus** (a noun) dressed in some stack of the
same recurring **modifier axes** (colour, material, texture, condition, size,
aura, ornament), plus a few **genus-locked** axes (an edible gets a *taste*; a
crop gets its Flayed-Sun *cultivar* descriptor). :func:`roll_trinket` picks a
genus from the zone, rolls a rarity, layers that many modifiers, and renders a
name in one of two registers:

* common    — ``[Aura] [Size] [Condition] [Colour] [Material] [Texture] GENUS``
* legendary — ``GENUS of the [Adjective] [Noun]``   (wondrous rolls only)
"""

from __future__ import annotations

import hashlib
import random
from typing import Optional

DEFAULT_BAR = 25  # monthly puntos needed to earn a trinket, unless reconfigured

# A single trinket's chance of being rolled from the month's featured ("in-season")
# zone. The remaining ~30% stray in from one of the *other* zones, drawn uniformly —
# so the featured taxon is a weighted *bonus*, never an exclusive monopoly.
FEATURED_WEIGHT = 0.70


# ---------------------------------------------------------------------------
# Deterministic seeding
# ---------------------------------------------------------------------------
def _seed(*parts: object) -> int:
    """A stable 64-bit seed from the given parts.

    Uses sha256 (not the salted builtin ``hash``) so the same inputs yield the
    same seed in every process and after every restart — the property the whole
    vitrine rests on.
    """
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")


# ---------------------------------------------------------------------------
# The modifier axes — merged pools that cross-apply to ANY genus
# ---------------------------------------------------------------------------
# Curated and combined from columns across Vaults of Vaarn (issues 1–4, Brogdog's
# Travelog) and Flayed Sun. These are the universal "spice"; a rarity roll
# decides how many get layered on.

COLOUR = [
    "Octarine", "Ulfire", "Glaucous", "Jade-green", "Champagne-pink", "Sable",
    "Xanthine", "Turquoise", "Crimson", "Indigo", "Lapis", "Viridian",
    "Rust-red", "Bone-white", "Void-black", "Platinum", "Iridescent", "Brindled",
    "Paisley", "Zebra-striped", "Milky", "Ochre", "Umber", "Vermilion",
    "Cobalt", "Saffron-yellow", "Wine-dark", "Verdigris", "Pearlescent", "Ashen",
]

MATERIAL = [
    "Onyx", "Basalt", "Obsidian", "Marble", "Amethyst", "Alabaster",
    "Petrified-wood", "Bone", "Black-glass", "Cacao", "Jade", "Serpentine",
    "Hematite", "Quartz", "Soapstone", "Brass", "Pumice", "Chalcedony",
    "Antimony", "Meerschaum", "Nacre", "Lacquered", "Waxen", "Salt-crystal",
    "Ivory", "Tarnished-silver", "Beaten-copper", "Amber", "Jet", "Terracotta",
]

TEXTURE = [
    "Velvet", "Quicksilver", "Rubbery", "Veined", "Waxy", "Glass-smooth",
    "Mirrored", "Gelatinous", "Powdery", "Clay-like", "Lustrous", "Opalescent",
    "Mosaic-like", "Cobwebbed", "Plush", "Spongy", "Vitreous", "Chitinous",
    "Downy", "Glitching", "Faceted", "Filigreed",
]

CONDITION = [
    "Shrivelled", "Sunbleached", "Mouldering", "Vine-wrapped", "Cracked",
    "Frozen-solid", "Desiccated", "Singed", "Rusted", "Skeletal",
    "Worryingly-fresh", "Surprisingly-intact", "Decrepit", "Waterlogged",
    "Salt-encrusted", "Moss-grown", "Worm-eaten", "Sun-faded", "Barnacled",
    "Ash-dusted", "Cobweb-veiled", "Half-melted",
]

SIZE = [
    "Trifling", "Diminutive", "Immense", "Outsized", "Thumb-sized", "Stunted",
    "Swollen", "Shrunken", "Hand-span", "Colossal", "Dainty", "Squat",
]

AURA = [
    "Haunted", "Cursed", "Blasphemous", "Dazzling", "Prismatic", "Withering",
    "Ghostly", "Mesmerising", "Sanctified", "Luminous", "Whispering",
    "Ill-omened", "Sorrowful", "Humming", "Lucky", "Forsaken", "Restless",
    "Beatific", "Uncanny", "Forbidden",
]

ORNAMENT = [
    "strung with little bells",
    "embedded with raw opals",
    "tiled with coloured glass",
    "inlaid with a single ruby eye",
    "wrapped in feathered wreaths",
    "engraved with tight spirals",
    "carved with twinned snakes",
    "stamped with a falling star",
    "crowned with dried flowers",
    "bound in fine human hair",
    "set with a small black mirror",
    "studded with milk-teeth",
    "gilded at every edge",
    "scrimshawed with a forgotten battle",
    "hung with a dozen tiny chains",
    "pricked all over with gold pins",
]

# Edible-locked — only produce (and the odd foodstuff) tastes of anything.
TASTE = [
    "bittersweet", "peppery", "sappy and sweet", "acrid", "smoky", "creamy",
    "mouth-puckeringly sour", "starchy", "watery", "gristly", "numbing on the tongue",
    "smelling faintly of rain", "reeking of rotten vegetation", "grassy",
    "tasting of warm copper", "cloying", "ashen on the finish",
]

# Optional closing sentence for rarer finds.
PROVENANCE = [
    "Kept as a keepsake from a corpse's forehead.",
    "Of interest, they say, to certain collectors.",
    "Its ink is still faintly luminous.",
    "Salvaged from a downed sky-barge.",
    "Pried from the breastplate of a broken golem.",
    "It is said to whisper at night; no one believes it.",
    "Still warm, for reasons no one will explain.",
    "Traded for at a ruinous loss, and quietly regretted.",
    "Older than the oldest surviving map.",
    "Unearthed where the dunes had wandered off.",
    "Blessed once, then very quietly deconsecrated.",
    "The last of its kind, in all probability.",
    "Won at Faacube against a man with no name.",
    "Exhumed from the Royal Gardens after the menagerie was loosed.",
    "Smuggled, against good advice, past three checkpoints.",
]

# Legendary naming — "GENUS of the [Adjective] [Noun]".
LEGEND_ADJ = [
    "Empty-Minded", "Miniature", "Putrefying", "Forgotten", "Weeping", "Gilded",
    "Patient", "Sixth", "Drowned", "Laughing", "Sleepless", "Hollow", "Blind",
    "Threadbare", "Unnumbered", "Cackling", "Faceless", "Velvet", "Glass",
]
LEGEND_NOUN = [
    "Sage", "Autarch", "Beast", "Quetzal", "Jaguar", "Sandworm", "Concubine",
    "Emperor", "Pilgrim", "Cartographer", "Heretic", "Widow", "Gambler", "Sun",
    "Continuum", "Sphinx", "Locust", "Serpent", "Mantis", "Yurling",
]
# Proper names for the alternate "the [Name] [Genus]" legendary form.
PROPER_NAME = [
    "Cossmoss", "Achefoot", "Mandala", "Mirage", "Zofi", "Antimony",
    "Chalcedony", "Meerschaum", "Glaucon", "Pirrip", "Dovenglass", "Purplebeck",
    "Froswhirl", "Rendmoor", "Lakspur", "Corabellia", "Ambrose", "Clotfish",
]

# Order modifiers render in, so a stack always reads grammatically.
_RENDER_ORDER = ["aura", "size", "condition", "colour", "material", "texture"]
_UNIVERSAL = ["colour", "material", "texture", "condition", "size"]
_AXIS_POOL = {
    "colour": COLOUR, "material": MATERIAL, "texture": TEXTURE,
    "condition": CONDITION, "size": SIZE, "aura": AURA,
}


# ---------------------------------------------------------------------------
# The zones (item taxa) — each a rotating "genus class"
# ---------------------------------------------------------------------------
# A zone supplies the genus nouns and declares which genus-locked axes it opens.
# The four Flayed-Sun produce zones (and the Succulent Salvage) additionally
# carry a `crop` column (cultivar / salvage descriptors) that is *always*
# attached — the "Cocoa Bean Table" feel: in the Bean Zone you roll the bean
# column every time, then optionally season with universal modifiers. The Lost
# Mint instead always-attaches a strike (figure, year, country) from its own
# tables. Two zones landed in 2026-09 (succulent, coin), taking the roster
# from 10 to 12 so a calendar year is one full cycle; months before that stay
# on the original 10-zone wheel so closed vitrines never redraw.

_BEAN_CROP = [
    "with stalks that reach the clouds", "grown up around an obelisk",
    "sprawled over old sculptures", "rooted in a ruin", "green and faintly furred",
    "impossibly dewy", "whose pods quiver", "whose pods slowly rotate",
    "with a thousand beans to the pod", "with a single bean to its pod",
    "trellised into a little maze", "trellised into a staircase", "hollow as a reed",
    "unnervingly dense", "jaguar-spotted", "boldly striped",
    "with a crystal hidden in the pod", "that glow faintly through the husk",
]
_PEPPER_CROP = [
    "tiny as a lunula", "coiled in small spirals", "long and thin",
    "shaped like a severed finger", "plump and deformed", "perfectly globular",
    "eerily geometric", "with microscopic seeds", "with needle-fine seeds",
    "with seeds that hiss in open air", "with seeds that ignite in open air",
    "purple shot with red", "green shot with orange", "of a brilliant yellow",
    "translucent white", "numbing to the tongue", "frankly psychedelic",
    "mild and unexpectedly sweet", "so spicy it is kept only for torture",
    "exhaling a fog only the Huhuahua can stand",
]
_CORN_CROP = [
    "with jade-green kernels", "with pale blue kernels", "with navy-and-gold kernels",
    "with kernels of pure black", "with red-and-gold kernels",
    "with metallic silver kernels", "with skull-shaped kernels",
    "with prismatic kernels", "with pyramidal kernels", "with prickly kernels",
    "on a scimitar-curved cob", "on an absurdly long cob", "on a coiling cob",
    "weeping a clear nectar", "on a towering stalk", "on a bone-white stalk",
    "sheathed in floating silk", "sheathed in matted, filthy silk",
    "with silk long enough for a wig", "passed off as counterfeit cocoa",
]
# Salvage-locked — always attached in the Succulent Salvage. Deliberately avoids
# the genera (bouba/kiki/greeble/nurnie/2d/facet/crystal, plus the later
# morphology/artifact types): those words live on the noun.
_SUCCULENT_CROP = [
    "potted in a rusted ration-tin", "growing from a cracked sun-disk",
    "with roots clutching a spent shell-casing", "sand-scoured on the windward face",
    "holding a bead of water that will not fall",
    "grafted onto a second, unwilling plant",
    "blooming a flower the colour of old brass",
    "with offsets arranged like hull-rivets",
    "hollow, a wasp-priest nested once",
    "stencilled with a serial number, half-faded",
    "wired into a dead console as if to cool it", "oozing a sap that sets as amber",
    "potted in a goblet from the Fifth Temple", "still tagged from the Royal Gardens",
    "pruned by something with very small shears",
    "leaning toward a sun that is not ours",
    "lashed to a sky-barge bulkhead with copper wire", "veined with solder",
    "lodged in a cracked helmet", "with a nameplate riveted to the pot",
    "caked in sky-barge dust", "nursed under a cracked cloche of black glass",
    "with a drip-tray hammered from a shield-boss",
    "tied with a pilgrim's faded ribbon", "its soil glittering with filing-dust",
    "with a single leaf gilt by some previous owner",
    "humming a scale no instrument uses", "watered, once, with ink",
    "sold as a cooling-fin and never corrected",
    "braced with a copper armature", "labelled in three dead scripts",
    "the soil still warm from the kiln",
    "bearing a customs-stamp from a port that sank",
]

ZONES: dict[str, dict] = {
    "bean": {
        "emoji": "🫘", "name": "the Bean Zone",
        "blurb": "beans of every persuasion",
        "genera": ["Bean", "Bean-pod", "Pod", "Cacao-bean", "Cluster of beans"],
        "crop": _BEAN_CROP, "edible": True,
    },
    "pepper": {
        "emoji": "🌶️", "name": "the Pepper Patch",
        "blurb": "peppers fair and infernal",
        "genera": ["Pepper", "Chile", "Peppercorn", "Pepper-pod", "Sprig of peppers"],
        "crop": _PEPPER_CROP, "edible": True,
    },
    "corn": {
        "emoji": "🌽", "name": "the Maize Rows",
        "blurb": "uncanny ears of corn",
        "genera": ["Cob", "Ear of corn", "Kernel", "Corn-husk", "Maize-doll"],
        "crop": _CORN_CROP, "edible": True,
    },
    "orchard": {
        "emoji": "🍈", "name": "the Wild Orchard",
        "blurb": "the strange fruit of Tenoch",
        "genera": [
            "massive Squash", "minuscule Squash", "iridescent Squash",
            "flesh-coloured Squash", "shadowless Sunflower", "black-petalled Sunflower",
            "Avocado with an eyeball pit", "infested Avocado", "sour Sweet-potato",
            "Sweet-potato that is almost (not quite) a person",
            "Sweet-potato that used to be a person", "blood-red Tomato", "ruby Tomato",
            "pink Tomato", "cobwebbed Fruit", "boll of ultra-soft Cotton",
            "boll of black Cotton", "fiercely-guarded Cacao-pod",
        ],
        "crop": None, "edible": True,
    },
    "relic": {
        "emoji": "🏺", "name": "the Vaults",
        "blurb": "relics of the lost ages",
        "genera": [
            "Reliquary", "Saint's finger", "Preserved heart", "Crystal skull",
            "Levitating orb", "Idol", "Diadem", "Funerary mask", "Votive urn",
            "Sceptre", "Hand-mirror", "Hourglass", "Ring of an autarch",
            "Jade statuette of a sandworm", "Sacred flower, pressed", "Astrolabe",
        ],
        "turnkey": [
            "Apocalypse Glass — dark glass in which an alien culture goes about its day",
            "the Lying Mirror, which reflects only what its holder wishes were true",
            "a Dried Crypt-Lotus, plucked from a corpse's forehead and kept for luck",
            "Prison-Orb of the Miniature Beast, something pacing inside it still",
            "an Immaculate Bird — a small white songbird that sings inside your skull",
            "the Book of Sand, whose pages never show the same word twice",
            "Aegis of the Empty-Minded Sage, warm to the touch and humming softly",
            "a Waxen Poetry-Cylinder, its verse half-melted into nonsense",
            "an Ulfire Candle that burns with the ninth colour, the one with no name",
            "a plastic bag, very old, full of human teeth (labelled, alphabetically)",
        ],
    },
    "curio": {
        "emoji": "💎", "name": "the Bazaar",
        "blurb": "curios and contraband",
        "genera": [
            "Memory-crystal", "phial of Saffron", "string of Temple-bells",
            "uncut Jewel", "skein of Spider-silk", "jar of Medicinal honey",
            "set of Loaded dice", "Music-box", "Snuffbox", "Glowstone",
            "Hologram-fob", "stoppered vial of Pale ikor", "deck of fortune-cards",
            "tin of Ancient cigarettes", "folded Treasure-map",
        ],
        "turnkey": [
            "a Cartridge of Bottled Birdsong, dawn chorus of a forest now paved over",
            "Loaded Dice that always roll a poet's favourite number",
            "a jar of Honey that remembers the shape of the comb it was taken from",
            "a Memory-crystal holding one stranger's perfect, ordinary Tuesday",
            "a tiny Brass Orrery of a solar system that does not, anywhere, exist",
            "a Canary in a Cage, both rendered exquisitely in coloured glass",
        ],
    },
    "bestial": {
        "emoji": "🐾", "name": "the Menagerie",
        "blurb": "remnants of magnificent beasts",
        "genera": [
            "Feather of {art} {beast}", "Claw of {art} {beast}", "Tooth of {art} {beast}",
            "Pelt-scrap of {art} {beast}", "Bezoar of {art} {beast}", "Vertebra of {art} {beast}",
            "unhatched Egg of {art} {beast}", "Whisker of {art} {beast}",
            "Scrimshawed rib of {art} {beast}", "Glass eye of {art} {beast}",
        ],
        "beasts": [
            "quetzal", "jaguar", "land-whale", "velociraptor", "giant sloth",
            "feathered serpent", "great tortoise", "axolotl", "mammoth",
            "emperor penguin", "wolf-faced bat", "enormous salamander", "draft-horse",
            "gargantuan lobster", "half-stag-half-doe",
        ],
        "turnkey": [
            "the Singing Throat-pouch of a beast that learned three words of speech",
            "a Two-Headed coin-purse sewn from a single putrefying hide",
            "an Egg-sac, cool and translucent — best not to ask whose",
        ],
    },
    "devotional": {
        "emoji": "⛩️", "name": "the Shrine",
        "blurb": "icons, idols and heresies",
        "genera": [
            "Votive figurine", "House-amulet", "Prayer-knot", "Ancestor-portrait",
            "Sacred puppet", "Censer", "Icon-tile", "Grudge-fetish", "Ex-voto",
            "Reliquary-locket", "Devotional medal", "Worry-bead",
        ],
        "motif": [
            "a Falling Star", "a Crowned Skull", "an Hourglass", "Twinned Snakes",
            "a Black Orchid", "a Hollow Crown", "a Sacred Blade", "a White Serpent",
            "a Storm-cloud", "a Locust", "a Goat with too many eyes", "a Sun, weeping",
        ],
        "turnkey": [
            "a False Tepoztli, its little bronze head muttering of unpaid taxes",
            "a Sweet-Potato Saint, carved by a colony that has decided it is holy",
            "the deconsecrated Idol of a god whose name the priests filed off",
        ],
    },
    "inscribed": {
        "emoji": "📜", "name": "the Scriptorium",
        "blurb": "books, scrolls and tablets",
        "genera": [
            "Codex", "Scroll", "Clay tablet", "Poetry-cylinder", "Folio",
            "Pamphlet", "Wax-tablet", "Almanac", "Map-case", "Grimoire-leaf",
            "Sealed letter", "Ledger",
        ],
        "inscription": [
            "its ink still faintly luminous", "with a coded message inside the cover",
            "illuminated in gold leaf", "written in a tongue no one now reads",
            "annotated by an angry, long-dead hand", "with a half-finished poem on the flyleaf",
            "smelling powerfully of cedar and rot", "with a tiny weapon hidden in the spine",
            "dog-eared at a single, ominous page",
        ],
        "turnkey": [
            "the diary of a child who claims to have been born inside Scriberspace",
            "a peace-treaty between two cities, both of which have since vanished",
            "an Almanac predicting weather for a year that never came",
        ],
    },
    "vessel": {
        "emoji": "🔮", "name": "the Hall of Forms",
        "blurb": "vessels of impossible geometry",
        "genera": [
            "Orb", "Cube", "Prism", "Helix", "Ziggurat-in-miniature", "Klein-flask",
            "Skull-shaped vessel", "Cone (balanced point-down)", "Toroid", "Ovoid",
            "Star-pointed reliquary", "Many-lobed flask", "Trefoil knot", "Möbius band",
        ],
        "turnkey": [
            "a Penrose Triangle you can pick up but cannot, afterwards, put down right",
            "a Black-Glass Tesseract that is always slightly larger on the inside",
            "a Gyroscope that points, doggedly, at the Fifth Temple",
        ],
    },
    "succulent": {
        "emoji": "🌵", "name": "the Succulent Salvage",
        "blurb": "desert plants looted from wrecks and ruins",
        "genera": [
            "Bouba Succulent", "Kiki Succulent", "Greebled Succulent",
            "Nurnie Succulent", "2d Succulent", "Facet-cut Succulent",
            "Crystal Succulent", "Living-stone Succulent", "Windowpane Succulent",
            "Spiral Succulent", "Crested Succulent", "Monstrose Succulent",
            "Wireframe Succulent", "Tessellated Succulent", "Medusa Succulent",
            "Carrion Succulent", "Sand-cast Succulent", "Clockwork Succulent",
            "Split-rock Succulent", "Tiger-jaw Succulent", "Pearl-string Succulent",
            "Low-poly Succulent", "Lantern Succulent", "Nested Succulent",
        ],
        "crop": _SUCCULENT_CROP,
        "turnkey": [
            "a Bouba Succulent that has engulfed a complete toolkit from a downed sky-barge",
            "a Kiki Succulent that draws blood from anyone who tries to water it",
            "a Crystal Succulent whose facets replay a short, silent war",
            "a 2d Succulent used as a bookmark; the page behind it is still wet",
            "a Living-stone Succulent indistinguishable from the gravel until it blinks",
            "a Clockwork Succulent that ticks only in the presence of a lie",
            "a Carrion Succulent whose bloom smells of a market that closed a century ago",
            "a Windowpane Succulent showing a tiny, well-lit room in which someone is still packing",
            "a Medusa Succulent whose arms have each rooted in a different century",
            "a Wireframe Succulent you can hang a coat on, though it resents this",
        ],
    },
    "coin": {
        "emoji": "🪙", "name": "the Lost Mint",
        "blurb": "coins of countries that may or may not still exist",
        "genera": [
            "Sovereign", "Franc", "Peso", "Yen", "Goldmark", "Rupee",
            "Drachma", "Denarius", "Solidus", "Dollar", "Crown", "Ducat",
            "Florin", "Thaler", "Dirham", "Doubloon", "Aureus", "Farthing",
            "Mohur", "Oban", "Piece-of-eight", "Escudo", "Stater", "Tael",
            "Shekel", "Daric", "Tetradrachm", "Sestertius", "Obol",
            "Antoninianus", "Hyperpyron", "Koban", "Sycee", "Cash-coin",
            "Pagoda", "Guilder", "Guinea", "Noble", "Groat", "Ruble",
            "Dinar", "Piastre", "Ecu", "Louis-d'or", "Cob-coin", "Trade-dollar",
            "Knife-money", "Manilla", "Zecchino", "Jetton", "Fanam", "Birr",
            "Scudo", "Asper", "Livre", "Testoon",
        ],
        "figures": [
            "Queen Victoria", "Abraham Lincoln", "Napoleon Bonaparte",
            "Simón Bolívar", "Emperor Meiji", "Cleopatra VII", "Haile Selassie",
            "Catherine the Great", "Moctezuma II", "Charlemagne", "Saladin",
            "Hatshepsut", "Shaka", "Tokugawa Ieyasu", "Empress Theodora",
            "Mansa Musa", "Julius Caesar", "Hypatia", "Sitting Bull", "Akbar",
            "Sejong the Great", "Queen Nzinga", "Ashoka", "Boudica",
            "Ramesses II", "Suleiman", "Peter the Great", "Benito Juárez",
            "Sun Yat-sen", "Queen Liliuokalani", "Harald Bluetooth", "Zenobia",
            "Empress Wu Zetian", "Kublai Khan", "Maria Theresa",
            "Alexander the Great", "Hannibal", "Nefertiti", "Emperor Trajan",
            "Kamehameha I", "Genghis Khan", "Túpac Amaru II",
            "Cyrus the Great", "Darius I", "Croesus", "Constantine", "Justinian",
            "Eleanor of Aquitaine", "Elizabeth I", "Louis XIV", "Alfred the Great",
            "Cnut", "Baibars", "Shah Jahan", "Rani Lakshmibai", "Shivaji",
            "Oda Nobunaga", "Yi Sun-sin", "Empress Myeongseong",
            "Jayavarman VII", "Sundiata Keita", "Yaa Asantewaa", "Menelik II",
            "Amanirenas", "Toussaint Louverture", "José de San Martín",
            "Atahualpa", "Pachacuti", "Cuauhtémoc", "Pacal the Great",
            "Tecumseh", "Sequoyah", "Miguel Hidalgo", "Pedro II",
            "Kaahumanu", "Irene of Athens", "Frederick Barbarossa",
            "Vercingetorix", "Theodoric", "Askia Muhammad", "Queen Amina",
        ],
        "years": [
            "330 BCE", "269 BCE", "221 BCE", "196 BCE", "44 BCE", "27 BCE",
            "79", "312", "330", "476", "537", "622", "697", "751", "793", "800",
            "960", "1000", "1066", "1099", "1204", "1206", "1258", "1282",
            "1347", "1415", "1453", "1492", "1519", "1532", "1588", "1607",
            "1648", "1683", "1707", "1756", "1776", "1789", "1799", "1804",
            "1810", "1815", "1821", "1830", "1848", "1857", "1861", "1868",
            "1871", "1873", "1883", "1887", "1896", "1898", "1905", "1912",
            "1914", "1917", "1918", "1922", "1923", "1933", "1936", "1945",
            "1947", "1948", "1953", "1959", "1960", "1964",
        ],
        "countries": [
            "Britain", "France", "Mexico", "Japan", "Prussia", "Spain",
            "Sweden", "Ethiopia", "Peru", "China", "India", "Rome", "Egypt",
            "Persia", "Mali", "Hawaii", "Byzantium", "the Ottoman Empire",
            "the United States", "Greece", "Portugal", "Russia", "Denmark",
            "Morocco", "Korea", "Siam", "the Mughal Empire", "Axum", "Venice",
            "Florence", "the Netherlands", "Bolivia", "Austria", "Poland",
            "Ireland", "Zanzibar", "Genoa", "Hungary", "Bohemia", "Saxony",
            "Naples", "Sicily", "Carthage", "Lydia", "Parthia", "Tibet",
            "Ceylon", "Nepal", "the Kushan Empire", "Angkor", "the Pagan Kingdom",
            "Majapahit", "Songhai", "Benin", "Kilwa", "Haiti", "Brazil",
            "Chile", "Colombia", "Argentina", "Liberia", "Ryukyu", "Joseon",
            "Goryeo", "Trebizond", "Cyprus", "Malta", "Aragon", "Castile",
            "Granada", "the Delhi Sultanate", "Vijayanagara", "the Chola Empire",
            "Bukhara", "Samarkand", "Bactria", "Champa", "Tonga", "Tahiti",
        ],
        "turnkey": [
            "a 1933 Double Eagle, still warm from a mint the dunes swallowed",
            "an 1804 Silver Dollar whose reverse shows a coastline that has since receded into myth",
            "a denarius of Caesar with the Ides scratched over the date",
            "the last Hawaiian dollar of Queen Liliuokalani, smelling of salt and burnt sugar",
            "a Maria Theresa thaler, the date frozen at 1780 no matter the year it was struck",
            "a holey dollar with the punched centre still inside it, rattling",
            "a Cash-coin whose square hole frames a star that is not in the sky tonight",
            "a Cob-coin, badly struck, on which two kings are trying to occupy the same face",
            "a Manilla that will not come off once worn",
            "an electrum Stater of Croesus, still sweating a little gold",
        ],
    },
}

ZONE_KEYS = list(ZONES.keys())
# Frozen 10-zone roster used for every month before :data:`ZONE_EXPANSION_YM`.
# Append-only on purpose: inserting into ``ZONES`` above this pair would
# reshuffle closed vitrines.
_LEGACY_ZONE_KEYS = [
    "bean", "pepper", "corn", "orchard", "relic",
    "curio", "bestial", "devotional", "inscribed", "vessel",
]
ZONE_EXPANSION_YM = "2026-09"  # succulent + coin; 12-zone years from here


# ---------------------------------------------------------------------------
# Zone rotation — deterministic, stateless, never repeats within a cycle
# ---------------------------------------------------------------------------
def _month_index(ym: str) -> int:
    """Months since year 0 for a 'YYYY-MM' string (a stable ordinal)."""
    y, m = int(ym[:4]), int(ym[5:7])
    return y * 12 + (m - 1)


def _zone_keys_for(ym: str) -> list[str]:
    """The zone roster in force for ``ym`` — 10 keys before the 2026-09
    expansion, all 12 after. Off-season picks use the same list, so a closed
    month's vitrine cannot grow a succulent or a coin it never rolled."""
    if ym < ZONE_EXPANSION_YM:
        return _LEGACY_ZONE_KEYS
    return ZONE_KEYS


def _cycle_order(keys: list[str], ym: str) -> tuple[list[str], int]:
    """Shuffled roster and position for ``ym`` under ``keys``."""
    idx = _month_index(ym)
    n = len(keys)
    cycle, pos = divmod(idx, n)
    order = list(keys)
    random.Random(_seed("zone-cycle", cycle)).shuffle(order)
    return order, pos


def zone_for_month(ym: str) -> str:
    """The active zone key for month ``ym`` ('YYYY-MM').

    Each block of ``len(keys)`` consecutive months is one *cycle*: the zone
    order within a cycle is a shuffle seeded by the cycle number, so every zone
    appears exactly once per cycle (true rotation), the order is unpredictable,
    and no zone repeats back-to-back *within* a cycle — all with no stored state.
    From :data:`ZONE_EXPANSION_YM` the roster is 12, so a calendar year is one
    cycle; earlier months keep the original 10-zone wheel. 2026's remaining
    four months take the four zones that wheel hadn't featured yet (leftover
    old + succulent + coin), in 12-shuffle order, so the cutover year is still
    twelve distinct seasons.
    """
    keys = _zone_keys_for(ym)
    order, pos = _cycle_order(keys, ym)
    if ZONE_EXPANSION_YM <= ym < "2027-01":
        used = set()
        for m in range(1, 9):
            o, p = _cycle_order(_LEGACY_ZONE_KEYS, f"2026-{m:02d}")
            used.add(o[p])
        leftover = [z for z in order if z not in used]
        return leftover[int(ym[5:7]) - 9]
    return order[pos]


def zone_label(key: str) -> str:
    return ZONES[key]["name"]


def zone_emoji(key: str) -> str:
    return ZONES[key]["emoji"]


def zone_blurb(ym: str, bar: int, *, past: bool = False) -> str:
    """A one-line announcement of a month's zone, for the leaderboard."""
    key = zone_for_month(ym)
    z = ZONES[key]
    if past:
        return f"{z['emoji']} _{ym} was {z['name']} — {z['blurb']}._"
    return (
        f"{z['emoji']} _{ym}: **{z['name']}** is in season — every **{bar} puntos** you "
        f"clear earns a trinket, most from it, the odd one straying in from afar._"
    )


# ---------------------------------------------------------------------------
# The roller
# ---------------------------------------------------------------------------
def _title(words: list[str]) -> str:
    """Join non-empty modifier words + genus into a Title-Cased display name."""
    return " ".join(w for w in words if w)


def roll_trinket(rng: random.Random, zone: dict) -> dict:
    """Roll one trinket from ``zone`` using ``rng``. Pure: same rng state in,
    same trinket out. Returns a plain dict (see :func:`render`)."""
    # ~10% of rolls in a Vaarn-flavoured zone surface a hand-written "rare find".
    turnkey = zone.get("turnkey")
    if turnkey and rng.random() < 0.10:
        text = turnkey[rng.randrange(len(turnkey))]
        return {"rarity": "wondrous", "turnkey": True, "display": text,
                "name": text, "tail": "", "provenance": ""}

    roll = rng.random()
    rarity = "humble" if roll < 0.55 else "fine" if roll < 0.90 else "wondrous"
    n_mods = {"humble": 1, "fine": 2, "wondrous": 3}[rarity]

    genus = zone["genera"][rng.randrange(len(zone["genera"]))]
    if "{beast}" in genus:
        beast = zone["beasts"][rng.randrange(len(zone["beasts"]))]
        art = "an" if beast[:1].lower() in "aeiou" else "a"
        genus = genus.format(beast=beast, art=art)

    # Universal modifier stack: n distinct axes, each a value from its pool.
    axes = list(_UNIVERSAL)
    rng.shuffle(axes)
    mods: dict[str, str] = {}
    for axis in axes[:n_mods]:
        pool = _AXIS_POOL[axis]
        mods[axis] = pool[rng.randrange(len(pool))]

    # Aura: sometimes on a fine roll, always on a wondrous one.
    if rarity == "wondrous" or (rarity == "fine" and rng.random() < 0.35):
        mods["aura"] = AURA[rng.randrange(len(AURA))]

    # Genus-locked trailing clauses.
    tail_parts: list[str] = []
    if zone.get("crop"):
        tail_parts.append(zone["crop"][rng.randrange(len(zone["crop"]))])
    if zone.get("inscription"):
        tail_parts.append(zone["inscription"][rng.randrange(len(zone["inscription"]))])
    if zone.get("motif"):
        tail_parts.append("bearing " + zone["motif"][rng.randrange(len(zone["motif"]))])
    if zone.get("figures"):
        figure = zone["figures"][rng.randrange(len(zone["figures"]))]
        year = zone["years"][rng.randrange(len(zone["years"]))]
        country = zone["countries"][rng.randrange(len(zone["countries"]))]
        tail_parts.append(f"bearing {figure}")
        tail_parts.append(f"struck {year} in {country}")

    ornament = ""
    if rarity != "humble" and rng.random() < (0.7 if rarity == "wondrous" else 0.35):
        ornament = ORNAMENT[rng.randrange(len(ORNAMENT))]
    if ornament:
        tail_parts.append(ornament)

    if zone.get("edible") and rng.random() < 0.7:
        tail_parts.append(TASTE[rng.randrange(len(TASTE))])

    provenance = ""
    if rng.random() < (0.8 if rarity == "wondrous" else 0.15 if rarity == "fine" else 0.0):
        provenance = PROVENANCE[rng.randrange(len(PROVENANCE))]

    # Name. Wondrous items sometimes earn the legendary "GENUS of the Adj Noun"
    # register — but not edibles, and not a genus that already contains "of …"
    # (e.g. a bestial "Claw of a jaguar"), which would collide or double the "of".
    legendary = (rarity == "wondrous" and not zone.get("edible")
                 and " of " not in genus and rng.random() < 0.6)
    if legendary:
        head = genus.split()[-1]  # the bare noun, e.g. "Reliquary"
        if rng.random() < 0.5:
            name = (f"{head} of the {LEGEND_ADJ[rng.randrange(len(LEGEND_ADJ))]} "
                    f"{LEGEND_NOUN[rng.randrange(len(LEGEND_NOUN))]}")
        else:
            name = f"the {PROPER_NAME[rng.randrange(len(PROPER_NAME))]} {head}"
        # Legendary names wear their flavour in the tail only.
        mods = {k: v for k, v in mods.items() if k == "aura"}
        ordered = [mods.get("aura", "")]
        name = _title(ordered + [name])
    else:
        name = _title([mods.get(a, "") for a in _RENDER_ORDER] + [genus])

    tail = ""
    if tail_parts:
        tail = " " + ", ".join(tail_parts)

    return {
        "rarity": rarity, "turnkey": False, "genus": genus, "mods": mods,
        "name": name, "tail": tail, "provenance": provenance,
        "display": (name + tail).strip(),
    }


def zone_pick_for(guild_id: int, user_id: int, ym: str, idx: int) -> tuple[str, bool]:
    """Which zone a single trinket is rolled from, and whether it's *in-season*.

    The month's featured zone (:func:`zone_for_month`) is favoured — it wins with
    probability :data:`FEATURED_WEIGHT` — otherwise an off-season zone is drawn
    uniformly from the rest. So the featured taxon is a *bonus*, not a monopoly:
    most of a month's trinkets come from it, but the odd one strays in from afar.
    Deterministic per (guild, user, month, index), seeded in its own keyspace so
    the choice never perturbs the item roll itself.
    """
    keys = _zone_keys_for(ym)
    featured = zone_for_month(ym)
    rng = random.Random(_seed("zone-pick", guild_id, user_id, ym, idx))
    if rng.random() < FEATURED_WEIGHT:
        return featured, True
    others = [z for z in keys if z != featured]
    return others[rng.randrange(len(others))], False


def roll_for(guild_id: int, user_id: int, ym: str, idx: int = 0) -> dict:
    """The deterministic trinket a worker earns as their ``idx``-th award in ``ym``.

    A worker earns one trinket per whole multiple of the bar (50 puntos against a
    25-punto bar → two); ``idx`` (0-based) picks which one. Each trinket draws its
    own zone via :func:`zone_pick_for` — the featured zone ~70% of the time, an
    off-season zone the rest — then rolls an item from it. Same inputs → same
    trinket, forever; it carries its month, index, picked zone, and season flag.
    """
    zone_key, in_season = zone_pick_for(guild_id, user_id, ym, idx)
    rng = random.Random(_seed("trinket", guild_id, user_id, ym, zone_key, idx))
    t = roll_trinket(rng, ZONES[zone_key])
    t["month"] = ym
    t["idx"] = idx
    t["zone_key"] = zone_key
    t["in_season"] = in_season
    t["zone_emoji"] = ZONES[zone_key]["emoji"]
    t["zone_name"] = ZONES[zone_key]["name"]
    return t


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
_RARITY_MARK = {"humble": "", "fine": "✦ ", "wondrous": "✦✦ "}


def render_line(t: dict) -> str:
    """One compact vitrine line for a trinket dict from :func:`roll_for`."""
    mark = _RARITY_MARK.get(t.get("rarity", ""), "")
    body = f"{t['zone_emoji']} {mark}**{t['name']}**{t.get('tail', '')}"
    if t.get("provenance"):
        body += f"  _{t['provenance']}_"
    return body


def render(t: dict) -> str:
    """A trinket's full one-piece description (name + tail + provenance)."""
    s = t["display"]
    if t.get("provenance"):
        s += f" — {t['provenance']}"
    return s
