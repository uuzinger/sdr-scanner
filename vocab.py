"""
Shared ASR vocabulary hint for faster-whisper's initial_prompt.

Used by both worker.py (once validated) and compare_vocab_prompt.py, so the
prompt only ever lives in one place. Edit this file as you notice more
misses in transcripts — streets, place names, unit designators, agency
terms.
"""

INITIAL_PROMPT = (
    "Loudoun County Fire and Rescue and Sheriff's Office radio traffic. "
    "Streets include Braddock Road, Route 50, Loudoun County Parkway, "
    "Belmont Ridge Road, Riding Center Drive. Areas include Ashburn, Sterling, "
    "South Riding, Aldie, Brambleton, Chantilly, Leesburg. Units include "
    "Engine, Medic, Tower, Battalion, Rescue."
)
