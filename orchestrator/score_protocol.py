"""The room scoreboard's wire format, not a game implementation or acceptance test.

Each builder chooses the game and how earned progress maps onto this scale.
The reporter transfers the stored score unchanged. Nothing here grades gameplay.
"""

MAX_SCORE = 1000
SCORES_PATH = "api/scores"
