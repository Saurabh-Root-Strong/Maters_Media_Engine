"""Platform publishers. Add a class here to support a new platform."""

from .instagram import InstagramPublisher
from .linkedin import LinkedInPublisher
from .twitter import TwitterPublisher

PUBLISHERS = [TwitterPublisher(), InstagramPublisher(), LinkedInPublisher()]
BY_KEY = {p.key: p for p in PUBLISHERS}

__all__ = ["PUBLISHERS", "BY_KEY", "TwitterPublisher", "InstagramPublisher", "LinkedInPublisher"]
