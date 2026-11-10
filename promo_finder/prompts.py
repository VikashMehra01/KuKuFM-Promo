"""Load versioned prompt templates from ``prompts/*.md``.

File format::

    ### SYSTEM
    ...system text with $placeholders...
    ### USER
    ...user text with $placeholders...

``string.Template`` is used (not ``str.format``) so JSON examples with braces
need no escaping.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from string import Template

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


@dataclass(frozen=True)
class Prompt:
    name: str
    system: Template
    user: Template
    version: str  # short content hash, recorded in run_log.json

    def render(self, **values: object) -> tuple[str, str]:
        str_values = {k: str(v) for k, v in values.items()}
        return self.system.substitute(str_values), self.user.substitute(str_values)


@lru_cache(maxsize=None)
def load_prompt(name: str, prompt_dir: Path = PROMPT_DIR) -> Prompt:
    path = prompt_dir / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    if "### SYSTEM" not in text or "### USER" not in text:
        raise ValueError(f"{path} must contain '### SYSTEM' and '### USER' sections")
    system_part, user_part = text.split("### USER", 1)
    system_part = system_part.split("### SYSTEM", 1)[1]
    version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
    return Prompt(name, Template(system_part.strip()), Template(user_part.strip()), version)
