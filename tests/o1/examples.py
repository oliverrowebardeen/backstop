"""Fifteen hand-made checker cases, three per verdict.

They are written by hand, not drawn from data/, so they cannot leak the test
split. Each case has the checker's inputs, the verdict a careful reader would
give, and a plausible model reply used by the offline tests. The live script
`python -m tests.o1.live_examples` sends the same inputs to the real model.
"""
from __future__ import annotations

import json
from typing import Any

TODAY = "2026-11-14"


def _r(rank: int, title: str, url: str, snippet: str, published: str | None = None,
       engine: str = "tavily") -> dict[str, Any]:
    return {"rank": rank, "title": title, "url": url, "snippet": snippet, "published": published,
            "engine": engine}


def _reply(verdict: str, confidence: float, why: str, rank: int | None = None,
           rewrite: str | None = None) -> str:
    return json.dumps({"verdict": verdict, "confidence": confidence, "why": why, "rank": rank,
                       "rewrite": rewrite})


EXAMPLES: list[dict[str, Any]] = [
    # HIT
    {
        "id": "hit-hubble",
        "query": "Hubble Space Telescope launch date",
        "goal": "the date the Hubble Space Telescope was launched",
        "results": [
            _r(1, "Hubble Space Telescope - NASA Science", "https://science.nasa.gov/mission/hubble/",
               "The Hubble Space Telescope was launched on April 24, 1990, aboard the space shuttle Discovery."),
            _r(2, "Hubble's greatest images", "https://example-gallery.org/hubble",
               "A gallery of the most famous Hubble pictures."),
        ],
        "expected": "HIT",
        "reply": _reply("HIT", 0.95, "Result 1 gives the launch date, April 24, 1990.", rank=1),
    },
    {
        "id": "hit-adidas",
        "query": "Adidas revenue 2022",
        "goal": "Adidas group revenue for fiscal year 2022 in euros",
        "results": [
            _r(1, "adidas - Wikipedia", "https://en.wikipedia.org/wiki/Adidas",
               "Adidas AG is a German athletic apparel and footwear corporation headquartered in Herzogenaurach."),
            _r(2, "adidas Annual Report 2022: Group results", "https://report.adidas-group.com/2022/en/group-results",
               "In euro terms, revenues grew 1% to EUR 22,511 million in 2022."),
        ],
        "expected": "HIT",
        "reply": _reply("HIT", 0.9, "Result 2 states 2022 revenue of EUR 22,511 million.", rank=2),
    },
    {
        "id": "hit-opera-house",
        "query": "who designed the Sydney Opera House",
        "goal": "the architect who designed the Sydney Opera House",
        "results": [
            _r(1, "Sydney Opera House tours and tickets", "https://example-tours.com/sydney",
               "Book a guided tour of the Opera House and see behind the scenes."),
            _r(2, "Jorn Utzon | Sydney Opera House", "https://www.sydneyoperahouse.com/our-story/jorn-utzon",
               "Jorn Utzon, the Danish architect who designed the Sydney Opera House, won the 1957 design competition."),
            _r(3, "Sydney harbour walks", "https://example-walks.com/harbour", "Ten walks around the harbour."),
        ],
        "expected": "HIT",
        "reply": _reply("HIT", 0.94, "Result 2 names Jorn Utzon as the designer.", rank=2),
    },
    # STALE
    {
        "id": "stale-un",
        "query": "current UN Secretary-General",
        "goal": "the person who is Secretary-General of the United Nations today",
        "results": [
            _r(1, "Ban Ki-moon appointed UN Secretary-General", "https://example-news.com/2006/ban",
               "The General Assembly appointed Ban Ki-moon of the Republic of Korea as the eighth Secretary-General.",
               published="2006-10-13"),
            _r(2, "The role of the Secretary-General", "https://example-civics.org/un-sg",
               "The Secretary-General is chief administrative officer of the UN.", published="2008-03-02"),
        ],
        "expected": "STALE",
        "reply": _reply("STALE", 0.86, "Results are from 2006 to 2008; the goal asks who holds the post in 2026.",
                        rewrite="UN Secretary-General 2026"),
    },
    {
        "id": "stale-iphone",
        "query": "newest iPhone model",
        "goal": "the newest iPhone model Apple sells as of today",
        "results": [
            _r(1, "Apple unveils iPhone 13 Pro", "https://example-tech.com/2021/iphone-13",
               "Apple today introduced iPhone 13 Pro and iPhone 13 Pro Max.", published="2021-09-14"),
        ],
        "expected": "STALE",
        "reply": _reply("STALE", 0.88, "The only result covers the 2021 iPhone 13 launch.",
                        rewrite="latest iPhone model 2026"),
    },
    {
        "id": "stale-bitcoin",
        "query": "bitcoin price",
        "goal": "the price of Bitcoin in US dollars this week",
        "results": [
            _r(1, "Bitcoin tops $70,000", "https://example-markets.com/2024/btc-70k",
               "Bitcoin rose above $70,000 for the first time on Friday.", published="2024-03-08"),
            _r(2, "Bitcoin price history", "https://example-markets.com/btc-history",
               "Yearly bitcoin prices from 2010 to 2024.", published="2024-12-31"),
        ],
        "expected": "STALE",
        "reply": _reply("STALE", 0.9, "Prices shown are from 2024, not this week.",
                        rewrite="bitcoin price USD November 2026"),
    },
    # WRONG_ENTITY
    {
        "id": "wrong-jaguar",
        "query": "jaguar top speed",
        "goal": "the top speed of the jaguar, the big cat",
        "results": [
            _r(1, "Jaguar F-Type R top speed tested", "https://example-cars.com/jaguar-f-type",
               "The Jaguar F-Type R reaches a top speed of 186 mph."),
            _r(2, "Jaguar Land Rover results", "https://example-business.com/jlr",
               "Jaguar Land Rover reported higher quarterly sales."),
        ],
        "expected": "WRONG_ENTITY",
        "reply": _reply("WRONG_ENTITY", 0.92, "Results cover Jaguar cars, not the animal.",
                        rewrite="jaguar Panthera onca top running speed"),
    },
    {
        "id": "wrong-jordan",
        "query": "Michael Jordan research",
        "goal": "the main research field of Michael I. Jordan, the UC Berkeley professor",
        "results": [
            _r(1, "Michael Jordan stats and career", "https://example-sports.com/michael-jordan",
               "Michael Jordan won six NBA championships with the Chicago Bulls."),
        ],
        "expected": "WRONG_ENTITY",
        "reply": _reply("WRONG_ENTITY", 0.93, "The result is about the basketball player, not the professor.",
                        rewrite="Michael I. Jordan UC Berkeley professor research field"),
    },
    {
        "id": "wrong-delta",
        "query": "Delta founded year",
        "goal": "the year Delta Faucet Company was founded",
        "results": [
            _r(1, "Delta Air Lines history", "https://example-airlines.com/delta-history",
               "Delta Air Lines traces its roots to a crop dusting operation founded in 1925."),
            _r(2, "Delta Air Lines - Wikipedia", "https://en.wikipedia.org/wiki/Delta_Air_Lines",
               "Delta Air Lines is a major airline of the United States headquartered in Atlanta."),
        ],
        "expected": "WRONG_ENTITY",
        "reply": _reply("WRONG_ENTITY", 0.91, "Results are about Delta Air Lines, not Delta Faucet.",
                        rewrite="Delta Faucet Company founded year history"),
    },
    # SHALLOW
    {
        "id": "shallow-home-depot",
        "query": "Home Depot fiscal 2021 number of stores",
        "goal": "the number of stores Home Depot operated at the end of fiscal 2021",
        "results": [
            _r(1, "The Home Depot Form 10-K for fiscal year ended January 30, 2022",
               "https://ir.homedepot.com/financial-reports/annual-reports/2021",
               "Annual report pursuant to Section 13 or 15(d) of the Securities Exchange Act of 1934."),
            _r(2, "Find a Home Depot store near you", "https://www.homedepot.com/l/storeDirectory",
               "Use the store locator to find opening hours."),
        ],
        "expected": "SHALLOW",
        "reply": _reply("SHALLOW", 0.82, "Result 1 is the fiscal 2021 10-K, but shows no store count.",
                        rank=1, rewrite="Home Depot stores at end of fiscal 2021 total"),
    },
    {
        "id": "shallow-fields",
        "query": "first woman Fields Medal",
        "goal": "the name of the first woman to win the Fields Medal",
        "results": [
            _r(1, "List of Fields Medal winners", "https://example-math.org/fields-medal-winners",
               "A complete list of Fields Medal recipients since 1936, with their institutions."),
            _r(2, "What is the Fields Medal?", "https://example-explainers.com/fields",
               "The Fields Medal is awarded every four years to mathematicians under 40."),
        ],
        "expected": "SHALLOW",
        "reply": _reply("SHALLOW", 0.8, "Result 1 lists every winner, but the snippet names none.",
                        rank=1, rewrite="first woman to win the Fields Medal"),
    },
    {
        "id": "shallow-ljubljana",
        "query": "Ljubljana population 2021 census",
        "goal": "the population of the city of Ljubljana in the 2021 census",
        "results": [
            _r(1, "Population by settlements, census 2021 - Statistical Office of Slovenia",
               "https://www.stat.si/census2021/settlements",
               "Census 2021 results: population by settlements, Slovenia."),
        ],
        "expected": "SHALLOW",
        "reply": _reply("SHALLOW", 0.78, "The official 2021 census table is the right page, but shows no figure.",
                        rank=1, rewrite="Ljubljana population census 2021 figure"),
    },
    # DEAD
    {
        "id": "dead-cfo",
        "query": "Zytronic CFO 2018",
        "goal": "the chief financial officer of Zytronic plc in 2018",
        "results": [
            _r(1, "What does a CFO do?", "https://example-careers.com/cfo",
               "A chief financial officer manages a company's finances."),
            _r(2, "Top CFOs of 2018", "https://example-business.com/top-cfos-2018",
               "Our ranking of the best finance chiefs at large US companies."),
        ],
        "expected": "DEAD",
        "reply": _reply("DEAD", 0.86, "Results are generic CFO pages and never mention Zytronic.",
                        rewrite="Zytronic plc annual report 2018 finance director"),
    },
    {
        "id": "dead-hafnium",
        "query": "hafnium carbide melting point",
        "goal": "the melting point of hafnium carbide in degrees Celsius",
        "results": [
            _r(1, "Melting point - definition", "https://example-dictionary.com/melting-point",
               "The temperature at which a solid becomes a liquid."),
            _r(2, "Carbide drill bits for sale", "https://example-tools.com/carbide-bits",
               "Shop our range of carbide drill bits."),
        ],
        "expected": "DEAD",
        "reply": _reply("DEAD", 0.9, "Neither result is about hafnium carbide.",
                        rewrite="hafnium carbide HfC melting point Celsius"),
    },
    {
        "id": "dead-empty",
        "query": "Vellora Island ferry timetable 1952",
        "goal": "the ferry departure times to Vellora Island in 1952",
        "results": [],
        "expected": "DEAD",
        "reply": None,
    },
]
