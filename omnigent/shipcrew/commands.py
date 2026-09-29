"""Mission commands from the board's "Ask the crew" box.

``POST /missions/{id}/command {text}`` maps a short free-text order to one
board action with fixed rules (no LLM): accents and case are ignored, French
and English both work. The text must be one command verb plus optional filler
words ("all", "tout", "les tâches", "please", ...). Anything else, including a
negation ("don't run", "ne lance pas") or two verbs at once, is refused with the
list of supported commands, so a sentence never triggers an action by accident.

A real LLM orchestrator chat (free text to a mission-scoped ``shipcrew``
session) is a later step; this is its safe minimal version.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

Intent = Literal["start_all", "plan", "sync", "stop_all"]

# Verb -> intent. Every word is compared after accent and case folding.
_VERBS: dict[str, Intent] = {
    # run everything
    "run": "start_all",
    "start": "start_all",
    "launch": "start_all",
    "lance": "start_all",
    "lancer": "start_all",
    "lancez": "start_all",
    "demarre": "start_all",
    "demarrer": "start_all",
    "demarrez": "start_all",
    # plan from the repo's PRD
    "plan": "plan",
    "replan": "plan",
    "planifie": "plan",
    "planifier": "plan",
    "planifiez": "plan",
    # GitHub sync
    "sync": "sync",
    "resync": "sync",
    "synchronize": "sync",
    "synchronise": "sync",
    "synchroniser": "sync",
    "synchronisez": "sync",
    # stop the running tasks
    "stop": "stop_all",
    "halt": "stop_all",
    "arrete": "stop_all",
    "arreter": "stop_all",
    "arretez": "stop_all",
    "stoppe": "stop_all",
    "stopper": "stop_all",
    "stoppez": "stop_all",
}

# Words allowed around the verb. Negations ("don't", "ne", "pas", "not") are
# deliberately absent: they make the text unknown.
_FILLER = frozenset(
    {
        "all", "everything", "every", "the", "tasks", "task", "mission", "now", "please",
        "from", "prd", "github", "with", "it", "them", "again", "board", "crew", "cards",
        "tout", "toutes", "tous", "les", "la", "le", "l", "de", "du", "des", "taches",
        "tache", "maintenant", "svp", "stp", "s", "il", "te", "vous", "plait", "avec",
        "a", "partir", "cartes", "equipe", "encore",
    }
)  # fmt: skip

SUPPORTED_COMMANDS: tuple[str, ...] = (
    "run all / lance tout / démarre: move every backlog task to Ready",
    "plan / planifie: plan from the repo's PRD (.shipcrew/prd.md)",
    "sync / synchronise: sync GitHub issues and PRs now",
    "stop all / arrête tout: stop every running task",
)

MAX_COMMAND_CHARS = 2000


def normalize(text: str) -> list[str]:
    """Lowercase words of ``text`` with accents stripped and punctuation dropped."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()
    return re.findall(r"[a-z0-9]+", plain)


def classify(text: str) -> Intent | None:
    """The intent of a board command, or ``None`` when it matches no rule.

    >>> classify("Lance tout !")
    'start_all'
    >>> classify("don't run all") is None
    True
    """
    words = normalize(text)
    intents = {_VERBS[w] for w in words if w in _VERBS}
    if len(intents) != 1:
        return None
    if any(w not in _VERBS and w not in _FILLER for w in words):
        return None
    return intents.pop()


def unknown_command_message(text: str) -> str:
    """The 400 message listing what the command box understands."""
    shown = text.strip()
    if len(shown) > 80:
        shown = shown[:77] + "..."
    return f"Unknown command {shown!r}. Supported commands: " + "; ".join(SUPPORTED_COMMANDS)
