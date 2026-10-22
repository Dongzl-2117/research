from researchlib.web.routes import robots


def test_robots_allows_only_claude_user():
    body = robots().body.decode()
    groups = [g.strip().splitlines() for g in body.strip().split("\n\n")]
    assert groups[0] == ["User-agent: Claude-User", "Allow: /"]
    assert groups[1] == ["User-agent: *", "Disallow: /"]
    assert len(groups) == 2
