"""The SerpApi leaf tools: discovery tools + their paired detail tools.

Discovery → detail chains:
  amazon_search → amazon_product        (asin)
  youtube_search → youtube_transcript   (video id)
  google_maps_search → google_maps_reviews  (data_id)

The general leaves ship on both the main agent and sub-agents; the research-only engines
(flights, hotels, finance, scholar, maps_reviews) are wired onto the research sub-agent, not the
lean main roster — see docs/serpapi-search-tools.md "Roster tiers".
"""

from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, Field

from app.search.serp_tool import SerpTool, format_hits

# How much of ONE source a single read returns — a transcript here, a web page in `__init__.py`.
# One number, because the question is the same either way: how much of one source is worth carrying
# in a context budget that has to hold the conversation too.
MAX_SOURCE_CHARS = 20_000


class GoogleSearchTool(SerpTool):
    """Raw Google organic results — best when the owner wants source links or a specific site/page."""

    name = "google_search"
    one_line = "Search Google for organic links and snippets"
    description = (
        "Search Google and return organic results (links + snippets). "
        "Use for source hunting, site-specific searches (site:example.com), or when "
        "the owner wants the actual URLs. "
        "For a synthesized answer with citations, use google_ai_answer instead."
    )
    engine = "google"

    class Input(BaseModel):
        """Arguments for a Google web search."""

        query: str = Field(description='Search query; supports operators like site:, intitle:, "exact phrase"')

    def params(self, query: str) -> dict:  # type: ignore[override]  # noqa: ANON002 — SerpAPI query params; narrower sig is intentional
        """Map query → Google params."""
        return {"q": query, "gl": "us", "hl": "en"}

    def render(self, results: dict) -> str:  # noqa: ANON002 — SerpAPI JSON response, schema varies
        """Format answer_box (if present) + top organic results."""
        lines = []
        answer_box = results.get("answer_box")
        if answer_box:
            answer = answer_box.get("answer") or answer_box.get("snippet", "")
            if answer:
                lines.append(f"Direct answer: {answer}")
        hits = [
            {"": r.get("title", ""), "snippet": (r.get("snippet") or "")[:160], "link": r.get("link", "")}
            for r in results.get("organic_results", [])
        ]
        if hits:
            lines.append(format_hits("Google results", hits))
        return "\n\n".join(lines)


class GoogleAiModeTool(SerpTool):
    """Google AI Mode — synthesized answer with sources.

    Best for "explain / compare / what's the best…" questions.
    """

    name = "google_ai_answer"
    one_line = "Ask Google's AI for a synthesized answer with citations"
    description = (
        "Google AI Mode returns one synthesized answer (not a list of links) plus source references. "
        "Use for 'explain X', 'compare A and B', 'what's the best…' questions. "
        "For raw organic links, use google_search instead."
    )
    engine = "google_ai_mode"

    class Input(BaseModel):
        """Arguments for a Google AI Mode query."""

        query: str = Field(description="The question or topic to synthesize an answer for")

    def params(self, query: str) -> dict:  # type: ignore[override]  # noqa: ANON002 — SerpAPI query params; narrower sig is intentional
        """Map query → AI Mode params."""
        return {"q": query, "gl": "us", "hl": "en"}

    def render(self, results: dict) -> str:  # noqa: ANON002 — SerpAPI JSON response, schema varies
        """Return synthesized markdown + up to 5 source references."""
        lines = []
        markdown = results.get("reconstructed_markdown", "")
        if markdown:
            lines.append(markdown[:2000])
        sources = results.get("references") or results.get("sources") or []
        if sources:
            ref_lines = ["Sources:"]
            for src in sources[:5]:
                title = src.get("title", "")
                link = src.get("link", "")
                ref_lines.append(f"- {title} · {link}")
            lines.append("\n".join(ref_lines))
        return "\n\n".join(lines)


class GoogleMapsSearchTool(SerpTool):
    """Local business discovery via Google Maps."""

    name = "google_maps_search"
    one_line = "Search Google Maps for local businesses or places"
    description = (
        "Find local businesses, restaurants, shops, or landmarks via Google Maps. "
        "Returns name, rating, address, phone, and a data_id for further queries (e.g. reviews)."
    )
    engine = "google_maps"

    class Input(BaseModel):
        """Arguments for a Google Maps local search."""

        query: str = Field(description="What to search for, e.g. 'sushi restaurant San Francisco'")

    def params(self, query: str) -> dict:  # type: ignore[override]  # noqa: ANON002 — SerpAPI query params; narrower sig is intentional
        """Map query → Maps params."""
        return {"q": query, "hl": "en"}

    def render(self, results: dict) -> str:  # noqa: ANON002 — SerpAPI JSON response, schema varies
        """Format top local results with rating, address, and data_id."""
        hits = [
            {
                "": r.get("title", ""),
                "rating": str(r.get("rating", "")),
                "reviews": str(r.get("reviews", "")),
                "type": r.get("type", ""),
                "address": r.get("address", ""),
                "phone": r.get("phone", ""),
                "data_id": r.get("data_id", ""),
            }
            for r in results.get("local_results", [])
        ]
        return format_hits("Google Maps results", hits)


class GoogleNewsTool(SerpTool):
    """Current news headlines from Google News."""

    name = "google_news"
    one_line = "Search Google News for recent headlines"
    description = (
        "Scan current news headlines and sources on any topic. "
        "Use for breaking news, recent events, or topic monitoring."
    )
    engine = "google_news"

    class Input(BaseModel):
        """Arguments for a Google News search."""

        query: str = Field(description="Topic or keyword to search for in recent news")

    def params(self, query: str) -> dict:  # type: ignore[override]  # noqa: ANON002 — SerpAPI query params; narrower sig is intentional
        """Map query → News params."""
        return {"q": query, "gl": "us", "hl": "en"}

    def render(self, results: dict) -> str:  # noqa: ANON002 — SerpAPI JSON response, schema varies
        """Format news headlines with source, date, and link."""
        hits = [
            {
                "": r.get("title", ""),
                "source": (r.get("source") or {}).get("name") or r.get("source", ""),
                "date": r.get("date", ""),
                "link": r.get("link", ""),
            }
            for r in results.get("news_results", [])
        ]
        return format_hits("Google News results", hits)


class GoogleEventsTool(SerpTool):
    """Local events — concerts, shows, meetups — from Google Events."""

    name = "google_events"
    one_line = "Find local events (concerts, shows, meetups) via Google Events"
    description = (
        "Discover upcoming events by topic or location. "
        "Use for 'concerts in SF this weekend', 'tech meetups near me', etc."
    )
    engine = "google_events"

    class Input(BaseModel):
        """Arguments for a Google Events search."""

        query: str = Field(description="Event query, e.g. 'concerts in San Francisco this weekend'")

    def params(self, query: str) -> dict:  # type: ignore[override]  # noqa: ANON002 — SerpAPI query params; narrower sig is intentional
        """Map query → Events params."""
        return {"q": query, "hl": "en"}

    def render(self, results: dict) -> str:  # noqa: ANON002 — SerpAPI JSON response, schema varies
        """Format events with date, venue, and link."""
        hits = []
        for r in results.get("events_results", []):
            date = (r.get("date") or {}).get("when", "")
            address_raw = r.get("address") or r.get("venue", {}).get("name", "")
            address = ", ".join(address_raw) if isinstance(address_raw, list) else address_raw
            hits.append({"": r.get("title", ""), "date": date, "venue": address, "link": r.get("link", "")})
        return format_hits("Google Events results", hits)


class AmazonSearchTool(SerpTool):
    """Amazon product discovery — surfaces asin for amazon_product."""

    name = "amazon_search"
    one_line = "Search Amazon for products"
    description = (
        "Find products on Amazon by keyword. Returns title, price, rating, and asin. "
        "Use asin with amazon_product to fetch the full product card."
    )
    engine = "amazon"

    class Input(BaseModel):
        """Arguments for an Amazon product search."""

        query: str = Field(description="Product keywords, e.g. 'USB-C 100W charger'")

    def params(self, query: str) -> dict:  # type: ignore[override]  # noqa: ANON002 — SerpAPI query params; narrower sig is intentional
        """Map query → Amazon search params."""
        return {"k": query}

    def render(self, results: dict) -> str:  # noqa: ANON002 — SerpAPI JSON response, schema varies
        """Format Amazon results with price, rating, and asin."""
        hits = [
            {
                "": r.get("title", ""),
                "price": r.get("price") or str(r.get("extracted_price", "")),
                "rating": str(r.get("rating", "")),
                "reviews": str(r.get("reviews", "")),
                "asin": r.get("asin", ""),
            }
            for r in results.get("organic_results", [])
        ]
        return format_hits("Amazon results", hits)


class AmazonProductTool(SerpTool):
    """Amazon product detail — full card for one asin."""

    name = "amazon_product"
    one_line = "Fetch the full Amazon product card by asin"
    description = (
        "Retrieve the full product card for one Amazon item (price, rating, specs, feature bullets). "
        "Get the asin from amazon_search first."
    )
    engine = "amazon_product"

    class Input(BaseModel):
        """Arguments for fetching one Amazon product."""

        asin: str = Field(description="Amazon product id (asin), e.g. from amazon_search")

    def params(self, asin: str) -> dict:  # type: ignore[override]  # noqa: ANON002 — SerpAPI query params; narrower sig is intentional
        """Map asin → Amazon product params."""
        return {"asin": asin}

    def render(self, results: dict) -> str:  # noqa: ANON002 — SerpAPI JSON response, schema varies
        """Render the product card: title, price, rating, and first 5 feature bullets."""
        product = results.get("product_results") or results
        lines = []
        title = product.get("title", "")
        if title:
            lines.append(title)
        price = product.get("price") or product.get("buybox_winner", {}).get("price", "")
        rating = product.get("rating", "")
        reviews = product.get("reviews", "")
        if price or rating:
            lines.append(f"price: {price} · rating: {rating} · reviews: {reviews}")
        bullets = product.get("about_this_item") or product.get("feature_bullets") or []
        if bullets:
            lines.extend(["Features:", *[f"- {b}" for b in bullets[:5]]])
        return "\n".join(lines)


class YouTubeSearchTool(SerpTool):
    """YouTube video discovery — surfaces video id for youtube_transcript."""

    name = "youtube_search"
    one_line = "Search YouTube for videos"
    description = (
        "Find YouTube videos by query (creators, topics, tutorials). "
        "Returns title, channel, views, and video id. "
        "Use the id with youtube_transcript to read/summarize the video content."
    )
    engine = "youtube"

    class Input(BaseModel):
        """Arguments for a YouTube video search."""

        query: str = Field(description="What to search for on YouTube")

    def params(self, query: str) -> dict:  # type: ignore[override]  # noqa: ANON002 — SerpAPI query params; narrower sig is intentional
        """Map query → YouTube search params."""
        return {"search_query": query, "gl": "us", "hl": "en"}

    def render(self, results: dict) -> str:  # noqa: ANON002 — SerpAPI JSON response, schema varies
        """Format video results with channel, views, length, and video id."""
        hits = []
        for r in results.get("video_results", []):
            video_id = _parse_video_id(r.get("link", ""))
            hits.append(
                {
                    "": r.get("title", ""),
                    "channel": (r.get("channel") or {}).get("name", ""),
                    "views": str(r.get("extracted_views") or r.get("views", "")),
                    "length": r.get("length", ""),
                    "id": video_id,
                }
            )
        return format_hits("YouTube results", hits)


class YouTubeTranscriptTool(SerpTool):
    """Fetch the full transcript of a YouTube video — deliberately large output."""

    name = "youtube_transcript"
    one_line = "Get the transcript of a YouTube video"
    description = (
        "Fetch the full transcript of a video by its id (from youtube_search). "
        "Use to study, summarize, or quote a video's content without watching it."
    )
    engine = "youtube_video_transcript"

    class Input(BaseModel):
        """Arguments for fetching a YouTube transcript."""

        video_id: str = Field(description="YouTube video id (the v= value), e.g. from youtube_search")
        language_code: str = Field(default="en", description="Transcript language; defaults to en")

    def params(self, video_id: str, language_code: str = "en") -> dict:  # type: ignore[override]  # noqa: ANON002 — SerpAPI query params; narrower sig is intentional
        """Map video_id + language_code → transcript params."""
        return {"v": video_id, "language_code": language_code}

    def render(self, results: dict) -> str:  # noqa: ANON002 — SerpAPI JSON response, schema varies
        """Join transcript segments into plain prose, capped like any other single source."""
        segments = results.get("transcript") or []
        text = " ".join(s.get("snippet", "") for s in segments if s.get("snippet"))
        return text[:MAX_SOURCE_CHARS]


class GoogleJobsTool(SerpTool):
    """Job listings from Google Jobs."""

    name = "google_jobs"
    one_line = "Search Google Jobs for job listings"
    description = (
        "Find job postings by role and optional location. Returns title, company, location, schedule, and posting date."
    )
    engine = "google_jobs"

    class Input(BaseModel):
        """Arguments for a Google Jobs search."""

        query: str = Field(description="Job title or keywords, e.g. 'senior software engineer'")
        location: str = Field(default="", description="Optional location filter, e.g. 'San Francisco, CA'")

    def params(self, query: str, location: str = "") -> dict:  # type: ignore[override]  # noqa: ANON002 — SerpAPI query params; narrower sig is intentional
        """Map query + optional location → Jobs params."""
        p: dict = {"q": query, "hl": "en"}
        if location:
            p["location"] = location
        return p

    def render(self, results: dict) -> str:  # noqa: ANON002 — SerpAPI JSON response, schema varies
        """Format job listings with company, location, schedule, and posted date."""
        hits = []
        for r in results.get("jobs_results", []):
            ext = r.get("detected_extensions") or {}
            hits.append(
                {
                    "": r.get("title", ""),
                    "company": r.get("company_name", ""),
                    "location": r.get("location", ""),
                    "schedule": ext.get("schedule_type", ""),
                    "posted": ext.get("posted_at", ""),
                    "link": r.get("link", ""),
                }
            )
        return format_hits("Google Jobs results", hits)


class GoogleFlightsTool(SerpTool):
    """Flight routes + prices between two airports for a date (research-worker tool)."""

    name = "google_flights"
    one_line = "Search Google Flights for routes and prices"
    description = (
        "Find flights between two airports for a date: prices, durations, stops, airlines. "
        "Give IATA codes (e.g. CDG, JFK) and an outbound date; add a return date for a round trip."
    )
    engine = "google_flights"

    class Input(BaseModel):
        """Arguments for a Google Flights search."""

        departure_id: str = Field(description="Departure airport IATA code, e.g. 'CDG'")
        arrival_id: str = Field(description="Arrival airport IATA code, e.g. 'JFK'")
        outbound_date: str = Field(description="Outbound date, YYYY-MM-DD")
        return_date: str = Field(default="", description="Return date YYYY-MM-DD; empty = one-way")

    def params(self, departure_id: str, arrival_id: str, outbound_date: str, return_date: str = "") -> dict:  # type: ignore[override]  # noqa: ANON002 — SerpAPI query params; narrower sig is intentional
        """Map fields → Flights params; type=2 (one-way) unless a return date makes it round-trip."""
        p = {
            "departure_id": departure_id,
            "arrival_id": arrival_id,
            "outbound_date": outbound_date,
            "type": "2",
            "currency": "USD",
            "gl": "us",
            "hl": "en",
        }
        if return_date:
            p["return_date"] = return_date
            p["type"] = "1"
        return p

    def render(self, results: dict) -> str:  # noqa: ANON002 — SerpAPI JSON response, schema varies
        """Format best + other flights: route, price, duration, stops, airlines."""
        groups = (results.get("best_flights") or []) + (results.get("other_flights") or [])
        hits = []
        for r in groups:
            legs = r.get("flights") or []
            airlines = ", ".join(dict.fromkeys(leg.get("airline", "") for leg in legs if leg.get("airline")))
            dep = legs[0].get("departure_airport", {}).get("id", "") if legs else ""
            arr = legs[-1].get("arrival_airport", {}).get("id", "") if legs else ""
            hits.append(
                {
                    "": f"{dep}→{arr}" if dep and arr else "flight",
                    "price": str(r.get("price", "")),
                    "duration_min": str(r.get("total_duration", "")),
                    "stops": str(max(len(legs) - 1, 0)),
                    "airlines": airlines,
                }
            )
        return format_hits("Google Flights results", hits)


class GoogleHotelsTool(SerpTool):
    """Bookable stays with nightly rates for a date range (research-worker tool)."""

    name = "google_hotels"
    one_line = "Search Google Hotels for stays with rates"
    description = (
        "Find hotels/stays for a location and date range: nightly rate, rating, reviews, class. "
        "Give a location query and check-in/check-out dates. Maps finds places; this gives bookable rates."
    )
    engine = "google_hotels"

    class Input(BaseModel):
        """Arguments for a Google Hotels search."""

        query: str = Field(description="Where to stay, e.g. 'hotels in Lisbon'")
        check_in_date: str = Field(description="Check-in date, YYYY-MM-DD")
        check_out_date: str = Field(description="Check-out date, YYYY-MM-DD")
        adults: int = Field(default=2, description="Number of adults; defaults to 2")

    def params(self, query: str, check_in_date: str, check_out_date: str, adults: int = 2) -> dict:  # type: ignore[override]  # noqa: ANON002 — SerpAPI query params; narrower sig is intentional
        """Map fields → Hotels params."""
        return {
            "q": query,
            "check_in_date": check_in_date,
            "check_out_date": check_out_date,
            "adults": str(adults),
            "currency": "USD",
            "gl": "us",
            "hl": "en",
        }

    def render(self, results: dict) -> str:  # noqa: ANON002 — SerpAPI JSON response, schema varies
        """Format properties: name, type, nightly rate, rating, reviews, class."""
        hits = [
            {
                "": r.get("name", ""),
                "type": r.get("type", ""),
                "rate/night": (r.get("rate_per_night") or {}).get("lowest", ""),
                "rating": str(r.get("overall_rating", "")),
                "reviews": str(r.get("reviews", "")),
                "class": r.get("hotel_class", ""),
            }
            for r in results.get("properties", [])
        ]
        return format_hits("Google Hotels results", hits)


class GoogleFinanceTool(SerpTool):
    """Live quote for a ticker/FX/crypto symbol (research-worker tool)."""

    name = "google_finance"
    one_line = "Get a live quote from Google Finance"
    description = (
        "Fetch a live quote for a ticker, index, FX pair, or crypto: price and movement. "
        "Use a Google Finance symbol like 'GOOGL:NASDAQ', 'EUR-USD', or 'BTC-USD'."
    )
    engine = "google_finance"

    class Input(BaseModel):
        """Arguments for a Google Finance quote."""

        query: str = Field(description="Google Finance symbol, e.g. 'GOOGL:NASDAQ', 'EUR-USD', 'BTC-USD'")

    def params(self, query: str) -> dict:  # type: ignore[override]  # noqa: ANON002 — SerpAPI query params; narrower sig is intentional
        """Map symbol → Finance params."""
        return {"q": query, "hl": "en"}

    def render(self, results: dict) -> str:  # noqa: ANON002 — SerpAPI JSON response, schema varies
        """One line: title, price, movement — a single quote, not a list."""
        summary = results.get("summary") or {}
        move = summary.get("price_movement") or {}
        parts = [summary.get("title", "")]
        price = summary.get("price") or str(summary.get("extracted_price", ""))
        if price:
            parts.append(f"price: {price}")
        if move:
            parts.append(f"change: {move.get('movement', '')} {move.get('percentage', '')}%")
        return " · ".join(p for p in parts if p)


class GoogleScholarTool(SerpTool):
    """Scholarly papers with citation counts (research-worker tool)."""

    name = "google_scholar"
    one_line = "Search Google Scholar for academic papers"
    description = (
        "Find peer-reviewed papers and preprints: title, authors/venue/year, citation count, link. "
        "Use for scholarly evidence, not general web results (use google_search for those)."
    )
    engine = "google_scholar"

    class Input(BaseModel):
        """Arguments for a Google Scholar search."""

        query: str = Field(description="Scholarly query; supports author: and source: operators")

    def params(self, query: str) -> dict:  # type: ignore[override]  # noqa: ANON002 — SerpAPI query params; narrower sig is intentional
        """Map query → Scholar params."""
        return {"q": query, "hl": "en"}

    def render(self, results: dict) -> str:  # noqa: ANON002 — SerpAPI JSON response, schema varies
        """Format results: title, publication summary, citation count, link."""
        hits = []
        for r in results.get("organic_results", []):
            cited = ((r.get("inline_links") or {}).get("cited_by") or {}).get("total", "")
            hits.append(
                {
                    "": r.get("title", ""),
                    "pub": ((r.get("publication_info") or {}).get("summary") or "")[:120],
                    "cited_by": str(cited),
                    "link": r.get("link", ""),
                }
            )
        return format_hits("Google Scholar results", hits)


class GoogleMapsReviewsTool(SerpTool):
    """Full review text for one place — the detail half of the maps chain (research-worker tool)."""

    name = "google_maps_reviews"
    one_line = "Read reviews for a place found via google_maps_search"
    description = (
        "Fetch the actual review text and ratings for one place by its data_id. "
        "Get the data_id from google_maps_search first — this reads what reviewers wrote."
    )
    engine = "google_maps_reviews"

    class Input(BaseModel):
        """Arguments for fetching one place's reviews."""

        data_id: str = Field(description="Place data_id, e.g. from google_maps_search")

    def params(self, data_id: str) -> dict:  # type: ignore[override]  # noqa: ANON002 — SerpAPI query params; narrower sig is intentional
        """Map data_id → Maps Reviews params."""
        return {"data_id": data_id, "hl": "en"}

    def render(self, results: dict) -> str:  # noqa: ANON002 — SerpAPI JSON response, schema varies
        """Format reviews: reviewer, rating, date, review text."""
        hits = []
        for r in results.get("reviews", []):
            snippet = r.get("snippet") or (r.get("extracted_snippet") or {}).get("original", "")
            hits.append(
                {
                    "": (r.get("user") or {}).get("name", ""),
                    "rating": str(r.get("rating", "")),
                    "date": r.get("date", ""),
                    "review": (snippet or "")[:200],
                }
            )
        return format_hits("Google Maps reviews", hits)


def _parse_video_id(link: str) -> str:
    """Extract the `v=` value from a YouTube URL; return empty string if absent."""
    qs = parse_qs(urlparse(link).query)
    ids = qs.get("v", [])
    return ids[0] if ids else ""
