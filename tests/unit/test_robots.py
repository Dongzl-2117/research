from researchlib.web.routes import robots


def test_robots_allows_all_crawlers():
    assert robots().body.decode() == "User-agent: *\nAllow: /\n"
