"""Monkey-patch parsehub's YouTube parser to:
1. Enable node.js as JS runtime (required for YouTube auth challenges)
2. Write cookies to a real temp file instead of StringIO (more compatible)
"""

import tempfile
from pathlib import Path

from log import logger

logger = logger.bind(name="YtdlpPatch")

_cookie_tmpdir = tempfile.mkdtemp(prefix="ytdlp_cookies_")


def patch_youtube_parser():
    try:
        from parsehub.parsers.parser.youtube import YtbParse
    except ImportError:
        logger.warning("parsehub YouTube parser not found, skip patching")
        return

    _original_params = YtbParse.params.fget

    @property
    def patched_params(self):
        p = _original_params(self)
        # Enable node.js runtime for YouTube auth challenges
        p["js_runtimes"] = {"deno": {}, "nodejs": {}}
        # Replace StringIO with a real temp file for cookie compatibility
        if self.cookie:
            cookie_file = Path(_cookie_tmpdir) / "youtube_cookies.txt"
            netscape = self.to_netscape_cookie(self.cookie, "youtube.com")
            if netscape:
                cookie_file.write_text(netscape, encoding="utf-8")
                p["cookiefile"] = str(cookie_file)
        return p

    YtbParse.params = patched_params
    logger.info("YouTube parser patched: js_runtimes=deno,nodejs + real cookie file")
