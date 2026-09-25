# APOLLO — Greek — god of arts, music and expression
# Media: music, videos, entertainment

import subprocess
import webbrowser
import os
from core.marduk import OdinModule


class Apollo(OdinModule):
    MODULE_NAME = "APOLLO"
    LAYER = "PERSONALITY"

    def __init__(self, config: dict):
        super().__init__(config)

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "play_music",
                "description": "Play music by searching YouTube",
                "parameters": {
                    "query": {"type": "string", "description": "Song, artist or genre to play"}
                },
                "required": ["query"]
            },
            {
                "name": "play_local_file",
                "description": "Play a local audio or video file",
                "parameters": {
                    "path": {"type": "string", "description": "Full path to the media file"}
                },
                "required": ["path"],
                "internal_only": True
            },
            {
                "name": "open_spotify",
                "description": "Open Spotify in the browser",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "open_youtube",
                "description": "Open YouTube in the browser",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "play_radio",
                "description": "Open an online radio station",
                "parameters": {
                    "genre": {"type": "string", "description": "Music genre (e.g. jazz, lofi, classical)"}
                },
                "required": ["genre"],
                "internal_only": True
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "play_music": self._play_music,
            "play_local_file": self._play_local,
            "open_spotify": self._spotify,
            "open_youtube": self._youtube,
            "play_radio": self._radio,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[APOLLO] Error: {e}"
        return f"[APOLLO] Unknown skill: {skill_name}"

    def _play_music(self, query: str = "") -> str:
        url = f"https://www.youtube.com/results?search_query={query.replace(' ', '+')}"
        webbrowser.open(url)
        return f"Searching YouTube for: {query}."

    def _play_local(self, path: str = "") -> str:
        if not os.path.exists(path):
            return f"File not found: {path}"
        subprocess.Popen(["start", path], shell=True)
        return f"Playing: {os.path.basename(path)}."

    def _spotify(self) -> str:
        webbrowser.open("https://open.spotify.com")
        return "Opening Spotify."

    def _youtube(self) -> str:
        webbrowser.open("https://www.youtube.com")
        return "Opening YouTube."

    _RADIO_URLS = {
        "lofi":      "https://www.youtube.com/results?search_query=lofi+hip+hop+radio",
        "jazz":      "https://www.youtube.com/results?search_query=jazz+radio+live",
        "classical": "https://www.youtube.com/results?search_query=classical+music+radio",
        "focus":     "https://www.youtube.com/results?search_query=focus+study+music+radio",
    }

    def _radio(self, genre: str = "") -> str:
        url = self._RADIO_URLS.get(genre.lower())
        if not url:
            url = f"https://www.youtube.com/results?search_query={genre}+radio+live"
        webbrowser.open(url)
        return f"Opening {genre} radio."
