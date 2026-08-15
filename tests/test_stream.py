from pathlib import Path

from worker.stream import StreamParser, Throttle

FIXTURE = Path(__file__).parent / "fixtures" / "review_stream.jsonl"


def parse_all():
    p = StreamParser()
    events = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        ev = p.feed_line(line)
        if ev:
            events.append(ev)
    return events


def test_init_event_extracts_slash_commands_and_session():
    init = next(e for e in parse_all() if e.type == "init")
    assert init.session_id == "abc-123"
    assert "review" in init.slash_commands and "model" in init.slash_commands


def test_text_and_tool_events():
    events = parse_all()
    texts = [e.text for e in events if e.type == "text"]
    assert texts == ["我来审查这个仓库。", "继续分析。"]
    tools = [(e.tool_name, e.text) for e in events if e.type == "tool"]
    assert tools[0][0] == "Bash"
    assert "git log" in tools[0][1]
    assert tools[1][0] == "Read"


def test_result_event():
    r = parse_all()[-1]
    assert r.type == "result" and r.text == "审查完成：3 个问题。"
    assert r.cost_usd == 0.21 and r.is_error is False
    assert r.subtype == "success"     # I-3：subtype（error_max_* 等）必须解析到位


def test_malformed_line_ignored():
    p = StreamParser()
    assert p.feed_line("not json") is None
    assert p.feed_line("") is None
    assert p.feed_line('{"type":"stream_event","event":{"type":"ping"}}') is None


def test_throttle_window():
    t = Throttle(interval_s=2.5)
    assert t.allow(now=0.0) is True      # 第一条立即放行
    assert t.allow(now=1.0) is False     # 窗口内抑制
    assert t.allow(now=2.6) is True      # 出窗口放行
    assert t.allow(now=3.0) is False
