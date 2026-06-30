import os
from dotenv import load_dotenv

load_dotenv()


# =========================================================
# CORE BOT / DATABASE SETTINGS
# =========================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "airboss.db")


# =========================================================
# PRIVATE / EPHEMERAL VIEW SETTINGS
# =========================================================

PRIVATE_VIEW_TIMEOUT_SECONDS = 900


# =========================================================
# SERVER ROLE SETTINGS
# =========================================================

ADMIN_ROLE = 1514415305755983883
MISSION_EXECUTER_ROLE = 1514415258352091317
INSTRUCTOR_ROLE = 1515786621818245201
FLIGHT_LEAD_ROLE = 1514415198767812748
MISSION_QUALIFIED_ROLE = 1514415135186485248

# =========================================================
# RANK SETTINGS
# Put these in order from LOWEST rank to HIGHEST rank.
# If a user has multiple rank roles, the bot uses the highest one.
# =========================================================

RANK_ROLES = [
    {
        "rank": "Recruit",
        "role_id": 1514674842350125128,
    },
    {
        "rank": "Ensign",
        "role_id": 1514674950902911048,
    },
    {
        "rank": "LTJG",
        "role_id": 1514675012781740102,
    },
    {
        "rank": "LCDR",
        "role_id": 1514675076773970072,
    },
    {
        "rank": "CDR",
        "role_id": 1514675230851993825,
    },
    {
        "rank": "CAPT",
        "role_id": 1514675302310346872,
    },
    {
        "rank": "XO",
        "role_id": 1514675351530242239,
    },
    {
        "rank": "CO",
        "role_id": 1514675402210017422,
    },
    {
        "rank": "DCAG",
        "role_id": 1514675483202158804,
    },
    {
        "rank": "CAG",
        "role_id": 1514675632993337396,
    },
    {
        "rank": "ADM",
        "role_id": 1514675710353080391,
    },
    {
        "rank": "RADM",
        "role_id": 1514675767932616736,
    },
    {
        "rank": "SECNAV",
        "role_id": 1514676954970984661,
    },
]

DEFAULT_RANK = "Recruit"


# =========================================================
# OPERATION TEMPLATE SETTINGS
# =========================================================

OP_TYPES = [
    "Normal",
    "Mini",
    "Arcade",
    "Tournament",
    "Training",
]

# You can edit this list anytime.
# Discord selects can only show 25 options max.
AIRCRAFT_OPTIONS = [
    {"name": "AV-42C", "max_seats": 1},
    {"name": "F/A-26", "max_seats": 1},
    {"name": "F-45A", "max_seats": 1},
    {"name": "EF-24", "max_seats": 2},
    {"name": "T-55", "max_seats": 2},
    {"name": "AH-94", "max_seats": 2},
    {"name": "F-16C", "max_seats": 1},
    {"name": "A-10D", "max_seats": 1},
    {"name": "AH-6", "max_seats": 2},
]


# =========================================================
# TIMEZONE OPTIONS
# Discord dropdowns can only show up to 25 options.
# =========================================================

TIMEZONE_OPTIONS = [
    ("Eastern Time", "America/New_York"),
    ("Central Time", "America/Chicago"),
    ("Mountain Time", "America/Denver"),
    ("Pacific Time", "America/Los_Angeles"),
    ("Alaska Time", "America/Anchorage"),
    ("Hawaii Time", "Pacific/Honolulu"),
    ("UTC", "UTC"),
    ("United Kingdom", "Europe/London"),
    ("Ireland", "Europe/Dublin"),
    ("Central Europe", "Europe/Berlin"),
    ("Western Europe", "Europe/Paris"),
    ("Eastern Europe", "Europe/Helsinki"),
    ("Brazil", "America/Sao_Paulo"),
    ("Australia East", "Australia/Sydney"),
    ("Australia West", "Australia/Perth"),
    ("New Zealand", "Pacific/Auckland"),
]


# =========================================================
# REQUEST QUALIFICATION OPTIONS
# =========================================================

REFERRAL_OPTIONS = [
    "Reddit",
    "YouTube",
    "Discord - Official VTOL Server",
    "Discord - Other",
    "A Friend",
]

# Ordered availability times.
# The order matters because end time must come AFTER start time.
TIME_OPTIONS = [
    ("12 AM", "00:00"),
    ("1 AM", "01:00"),
    ("2 AM", "02:00"),
    ("3 AM", "03:00"),
    ("4 AM", "04:00"),
    ("5 AM", "05:00"),
    ("6 AM", "06:00"),
    ("7 AM", "07:00"),
    ("8 AM", "08:00"),
    ("9 AM", "09:00"),
    ("10 AM", "10:00"),
    ("11 AM", "11:00"),
    ("12 PM", "12:00"),
    ("1 PM", "13:00"),
    ("2 PM", "14:00"),
    ("3 PM", "15:00"),
    ("4 PM", "16:00"),
    ("5 PM", "17:00"),
    ("6 PM", "18:00"),
    ("7 PM", "19:00"),
    ("8 PM", "20:00"),
    ("9 PM", "21:00"),
    ("10 PM", "22:00"),
    ("11 PM", "23:00"),
]


# =========================================================
# INSTRUCTOR QUALIFICATION OPTIONS
# =========================================================

MIN_VTOL_HOURS = 25
PING_COOLDOWN_MINUTES = 15


# =========================================================
# SCHEDULE DEFAULTS FOR /scheduleop
# Times are Eastern Time / New York.
#
# weekday uses Python's datetime numbering:
# Monday=0, Tuesday=1, Wednesday=2, Thursday=3,
# Friday=4, Saturday=5, Sunday=6
# =========================================================

SCHEDULE_DEFAULT_TIMEZONE = "America/New_York"

SCHEDULE_DEFAULT_SLOTS = [
    {"label": "Saturday 2 PM ET", "weekday": 5, "hour": 14, "minute": 0},
    {"label": "Saturday 4 PM ET", "weekday": 5, "hour": 16, "minute": 0},
    {"label": "Sunday 2 PM ET", "weekday": 6, "hour": 14, "minute": 0},
    {"label": "Sunday 4 PM ET", "weekday": 6, "hour": 16, "minute": 0},
    {"label": "Monday 2 PM ET", "weekday": 0, "hour": 14, "minute": 0},
    {"label": "Friday 8 PM ET", "weekday": 4, "hour": 20, "minute": 0},
]

OP_EVENT_MEETING_VCS = [
    1514318655406604340,
    1511788093227663431,

]

SCHEDULE_EVENT_DURATION_HOURS = 2


# =========================================================
# PROMOTION ELIGIBILITY OPTIONS
# =========================================================

PROMOTION_MANAGED_RANKS = [
    "Recruit",
    "ENS",
    "LTJG",
    "LT",
    "LCDR",
    "CDR",
    "CAPT",
]

PROMOTION_REQUIREMENTS = {
    "ENS": {
        "total_ops": 1,
        "unique_ops": 1,
    },
    "LTJG": {
        "total_ops": 3,
        "unique_ops": 2,
    },
    "LT": {
        "total_ops": 6,
        "unique_ops": 3,
    },
    "LCDR": {
        "total_ops": 10,
        "unique_ops": 5,
    },
    "CDR": {
        "total_ops": 16,
        "unique_ops": 8,
    },
    "CAPT": {
        "total_ops": 24,
        "unique_ops": 12,
    },
}

PROMOTION_ANNOUNCEMENT_CHANNEL_ID = 1465203828386173105

# Single promotion announcement.
PROMOTION_SINGLE_TEMPLATE = (
    "🎖️ Congratulations {mention}! You have been promoted from "
    "**{old_rank}** to **{new_rank}**."
)

# Batch promotion announcement.
PROMOTION_BATCH_TEMPLATE = (
    "🎖️ Congratulations to everyone promoted today!\n\n"
    "{promotion_lines}"
)

PROMOTION_BATCH_LINE_TEMPLATE = (
    "- {mention}: **{old_rank}** → **{new_rank}** "
    "({total_ops} total ops, {unique_ops} unique ops)"
)


# =========================================================
# FLIGHT VOICE-CHANNEL SETUP USED BY /start
# =========================================================
#
# Put the standard voice-channel IDs in the exact order you want flights assigned.
# First ID = Flight A / first template flight, second ID = Flight B, etc.
# Any channels left over after the template's flights are named "empty"
# with no user limit.

OP_FLIGHT_VOICE_CHANNEL_IDS = [
    1514318721877938286,  # First flight VC
    1514318763774840852,  # Second flight VC
    1514318793118318815,  # Third flight VC
    1514318835136598076,  # Fourth flight VC
    1514318925482033372,  # Fifth flight VC
    1514318966892531713,  # Sixth flight VC
    1517160821342339143,  # Seventh flight VC
    1517160846680264814,  # Eighth flight VC
]

# Flight A starts at this frequency. Each following flight adds the increment.
OP_FLIGHT_START_FREQUENCY = 200.0
OP_FLIGHT_FREQUENCY_INCREMENT = 5.0

# The background task waits this long between configured VC updates.
# Keep at least 1.0 to avoid rapid Discord channel rename bursts.
OP_FLIGHT_VC_UPDATE_DELAY_SECONDS = 1.2


# =========================================================
# SITUATION ROOM
# =========================================================

# Text channel where the bot posts reservation and attendance status boards.
SITUATION_ROOM_CHANNEL_ID = 1513972262582358087

# True: earliest upcoming op message is at the top of the channel.
# False: earliest upcoming op message is at the bottom.
SOONEST_ON_TOP = True

# Persistent JSON state. The bot creates and maintains this file automatically.
SITUATION_ROOM_STATE_FILE = "situation_room_messages.json"

# Multiple changes arriving inside this window are combined into one board refresh.
SITUATION_ROOM_UPDATE_DELAY_SECONDS = 5.0

# Delay between posts only when the bot must rebuild/reorder board messages.
SITUATION_ROOM_REORDER_DELAY_SECONDS = 1.0


# =========================================================
# REWARDS AND LEADERBOARD
# =========================================================

# Text channel where the bot maintains the rolling leaderboard messages.
LEADERBOARD_CHANNEL_ID = 1514334108225114234

# Rolling statistics use completed Normal operations whose scheduled date
# falls within this many days.
LEADERBOARD_WINDOW_DAYS = 50

# Persistent board-message state. Created and maintained automatically.
LEADERBOARD_STATE_FILE = "leaderboard_messages.json"

# Changes from /complete or /recordedit are combined for this long before
# the automatic reward reconciliation and leaderboard refresh run.
REWARD_RECONCILE_DELAY_SECONDS = 5.0
LEADERBOARD_UPDATE_DELAY_SECONDS = 5.0

# Ranking guardrails so one lucky record does not top a board.
LEADERBOARD_MIN_WIRE_SAMPLES = 5
RECENT_OP_LEADERBOARD_MIN_WIRE_SAMPLES = 2
LEADERBOARD_MIN_SURVIVAL_OPS = 10

# Display limits for the persistent leaderboard messages.
LEADERBOARD_TOP_LIMIT = 10
LEADERBOARD_AWARD_LIST_LIMIT = 25
LEADERBOARD_RECENT_HIGHLIGHT_DAYS = 25

# Carrier sections are shown in this exact order. Change the list later if you
# want to add AH-94, T-55, or other airframes.
LEADERBOARD_AIRFRAME_SECTIONS = [
    "F/A-26B",
    "EF-24G",
    "F-45A",
    "T-55",
    "AV-42C",
]

# Wire GPA scoring.
#
# Change only these numbers later if you want to rebalance GPA.
# Bolters count as their own GPA attempt using the BOLTER value.
WIRE_GPA_POINTS = {
    "BOLTER": 0.0,
    "ONE_WIRE": 1.0,
    "TWO_WIRE": 3.0,
    "THREE_WIRE": 4.0,
    "FOUR_WIRE": 2.0,
}

# Intro copy displayed above each Message 3 code block.
# The bot chooses "single" when there is one person/award in that section;
# otherwise it chooses "plural". Change any sentence to fit the server tone.
LEADERBOARD_DECORATION_INTROS = {
    "BATTLE_E": {
        "single": (
            "Congratulations to a pilot who performed a great feat "
            "during an operation!"
        ),
        "plural": (
            "Congratulations to a few pilots who performed great feats "
            "during their operations!"
        ),
    },
    "FIRST_TIME": {
        "single": "Congratulations to our first-time op attender!",
        "plural": "Congratulations to our first-time op attenders!",
    },
    "ACE": {
        "single": (
            "A pilot completed an operation without losing any aircraft "
            "and caught a 3 wire without boltering!"
        ),
        "plural": (
            "We have some pilots who completed operations without losing any "
            "aircraft and caught a 3 wire without boltering!"
        ),
    },
    "GOLDEN_WRENCH": {
        "single": (
            "A pilot received the award for 5 operations in a row "
            "without losing an aircraft."
        ),
        "plural": (
            "A few pilots received the award for 5 operations in a row "
            "without losing an aircraft."
        ),
    },
    "SAFETY_S": {
        "single": (
            "A pilot received an award for 5 clean arrested landings "
            "in a row without a single bolter."
        ),
        "plural": (
            "A few pilots received an award for 5 clean arrested landings "
            "in a row without a single bolter."
        ),
    },
}

# =========================================================
# MANUAL AWARD SETTINGS
# These names appear in /award user and /award revoke.
# Edit this list anytime.
# =========================================================

MANUAL_AWARDS = [
    "Battle E",
    "Purple Heart",
]


# =========================================================
# EW QUIZ
# =========================================================

EW_QUALIFIED_ROLE = 1518676620846694460
EW_QUIZ_TIME_LIMIT_MINUTES = 25
EW_QUIZ_JSON_PATH = "data/ew_quiz.json"

# How long someone must wait before retaking after Fail/Incomplete/Passed attempt.
# Use 0 to disable retake cooldown.
TEST_COOLDOWN_HOURS = 24

# Public congratulations channel for passing EW quiz.
EW_RESULTS_CHANNEL = 1465203828386173105

# Optional NATOPS message jump button shown on failed/cooldown/incomplete messages.
# Discord message links need both channel ID and message ID.
NATOPS_CHANNEL_ID = 1511787900482621591
NATOPS_MESSAGE_ID = 1515867916338200698

# =========================================================
# ASVAB QUIZ SETTINGS
# =========================================================

ASVAB_JSON_PATH = "data/asvab_quiz.json"

# Total number of questions to pull from the ASVAB question pool.
# The bot tries to pull an equal amount from each category.
# If a category does not have enough questions, it pulls all available
# from that category and fills remaining slots from other categories.
ASVAB_NUMBER_OF_QUESTIONS = 25
ASVAB_TIME_LIMIT_MINUTES = 60

# =========================================================
# FLIGHT LEAD REMINDERS
# =========================================================

# Send flight lead reservation reminders this many minutes before an event.
FLIGHTLEAD_REMINDER_MINUTES_BEFORE = 90

# How often the background reminder loop checks for due reminders.
FLIGHTLEAD_REMINDER_LOOP_SECONDS = 60


# =========================================================
# Training Signups
# =========================================================
TRAINING_TOPICS = [
    {"key": "case_1", "label": "Case 1"},
    {"key": "case_2", "label": "Case 2"},
    {"key": "case_3", "label": "Case 3"},
    {"key": "heli_case_1", "label": "Heli Case 1"},
    {"key": "a2g_weapons", "label": "A2G Weapons"},
    {"key": "a2a_combat", "label": "A2A Combat"},
    {"key": "ew", "label": "EW"},
]

TRAINING_DM_COOLDOWN_MINUTES = 2

TRAINING_ROSTER_VOICE_CATEGORY_IDS = [
    1519844972185256047,
    1511787800423301342
]

# =========================================================
# Name Pruning
# =========================================================
RANK_PRUNE_PREFIXES = [
    "HMLA-167",
    "VF-213",
    "VAQ-138",
    "VFA-97",
    "VFA-147",
    "VFA-31",
    "VMFA-323",
    "CAG",
    "DCAG",
    "CO",
    "XO",
    "ADM.",
    "RADM.",
    "CAPT.",
    "CDR.",
    "LCDR.",
    "LT.",
    "LTJG.",
    "ENS.",
    "CAG. ENS.",
    "Recruit",
]