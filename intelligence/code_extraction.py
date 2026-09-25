"""Extracting code from messy LLM responses.

When SARASWATI asks the cloud to fix or refactor a snippet, the model
usually wraps the answer in a ```fenced block``` — but not always. Sometimes:

  • The opening fence tags the wrong language (```py vs ```python).
  • The closing fence is missing (truncated mid-stream).
  • The reply is a unified diff instead of a full file.
  • Prose explanation leaks into the code block.
  • The reply is just raw code with no fence at all.

This module's job is to pull the fixed code out of any of those shapes,
returning enough metadata that the caller can decide whether the result is
trustworthy. Inspired by codebuff's iterative-extraction pattern.
"""

import re
from typing import Tuple


# A loose fence matcher that captures both the language tag and the body.
# Tolerates missing trailing fence (truncated streams).
_FENCE_RE = re.compile(
    r"```([a-zA-Z0-9_+\-]*)\s*\n(.*?)(?:```|\Z)",
    re.DOTALL,
)

# Aliases — LLMs frequently tag Python as `py`, JS as `node`, etc.
_LANG_ALIASES = {
    "py": "python", "py3": "python", "python3": "python",
    "js": "javascript", "node": "javascript", "jsx": "javascript",
    "ts": "typescript", "tsx": "typescript",
    "sh": "bash", "shell": "bash", "zsh": "bash",
    "cpp": "c++", "cxx": "c++",
    "yml": "yaml",
}

# Detect a unified-diff response shape so we can flag it rather than apply
# wholesale (applying user-targeted diffs out of band is risky).
_DIFF_MARKERS = ("--- ", "+++ ", "@@ ", "diff --git ")


def _normalize_lang(tag: str) -> str:
    return _LANG_ALIASES.get((tag or "").strip().lower(), (tag or "").strip().lower())


def looks_like_diff(text: str) -> bool:
    """Crude unified-diff sniff. True when at least one diff-style line marker
    appears at the start of a line — false otherwise."""
    head = text[:2000]
    return sum(int(any(line.startswith(m) for m in _DIFF_MARKERS))
               for line in head.splitlines()) >= 2


def _is_code_heuristic(line: str, language: str) -> bool:
    """Best-effort 'is this line code?' check used as the last-ditch parser.
    Catches the common case of an LLM dumping code with no fence at all."""
    s = line.lstrip()
    if not s or s.startswith("#") and language == "python":
        return True
    code_starters = {
        "python":     ("def ", "class ", "import ", "from ", "if ", "for ", "while ", "return ", "@"),
        "javascript": ("function ", "const ", "let ", "var ", "if (", "for (", "return ", "import ", "export "),
        "typescript": ("function ", "const ", "let ", "var ", "if (", "for (", "return ", "import ", "export ", "interface ", "type "),
        "java":       ("public ", "private ", "protected ", "class ", "void ", "if (", "for (", "return "),
        "c++":        ("int ", "void ", "class ", "struct ", "if (", "for (", "return ", "#include"),
        "rust":       ("fn ", "let ", "use ", "impl ", "struct ", "if ", "for ", "return "),
        "go":         ("func ", "var ", "if ", "for ", "return ", "import ", "package "),
    }
    starters = code_starters.get(_normalize_lang(language), ())
    return any(s.startswith(p) for p in starters)


def extract_fixed_code(response: str, language: str = "",
                      original: str = "") -> Tuple[str, str, str]:
    """Pull the fixed code out of an LLM response.

    Returns (code, strategy, explanation):
      code         — extracted code, possibly empty if nothing was salvageable
      strategy     — which path succeeded:
                       'strict-fence' | 'loose-fence' | 'truncated-fence' |
                       'diff' | 'heuristic' | 'raw'
      explanation  — prose that surrounded the code block in the response
                       (so the caller can show both the fix AND the rationale)
    """
    if not response or not response.strip():
        return "", "raw", ""

    lang = _normalize_lang(language)

    # 1. Strict fence — block whose language tag matches the request.
    #    The regex's |\Z alternative happily swallows truncated streams
    #    (LLM cut off mid-code), so verify a real closing ``` was present
    #    before claiming this was a clean strict-fence match.
    if lang:
        for m in _FENCE_RE.finditer(response):
            if _normalize_lang(m.group(1)) == lang:
                body = m.group(2).rstrip()
                if not body:
                    continue
                # _FENCE_RE.end() points past the closing fence OR end-of-string.
                # If end-of-string, no real close existed → truncated-fence.
                had_close = response[m.end() - 3:m.end()] == "```"
                explanation = (response[:m.start()] + response[m.end():]).strip()
                return body, ("strict-fence" if had_close else "truncated-fence"), explanation

    # 2. Loose fence — first fenced block regardless of tag.
    first = _FENCE_RE.search(response)
    if first:
        body = first.group(2).rstrip()
        if body:
            had_close = response[first.end() - 3:first.end()] == "```"
            strategy = "loose-fence" if had_close else "truncated-fence"
            explanation = (response[:first.start()] + response[first.end():]).strip()
            return body, strategy, explanation

    # 3. Diff detection. We surface the diff verbatim and let the caller
    #    decide — applying a unified diff blindly is dangerous when the
    #    line numbers might not match the user's local file.
    if looks_like_diff(response):
        return response.strip(), "diff", ""

    # 4. Heuristic: the entire response looks code-like (no prose).
    lines = response.splitlines()
    code_lines = [ln for ln in lines if ln.strip()]
    if code_lines:
        code_like = sum(1 for ln in code_lines if _is_code_heuristic(ln, lang))
        if code_like / max(1, len(code_lines)) > 0.6:
            return response.rstrip(), "heuristic", ""

    # 5. No structure recognized — hand the raw response back.
    return response.strip(), "raw", ""
