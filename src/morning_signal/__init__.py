"""Morning Signal — auto-generated daily briefing podcast.

Generates a podcast script via Claude with web search, converts it to audio
with Amazon Polly, publishes the MP3 + RSS feed to S3, and optionally emails
a success/failure notification via SES.
"""

__version__ = "0.1.1rc3"

__all__ = ["__version__"]
