from __future__ import annotations

from typing import Any

from tailwatch.connectors.base import CheckContext
from tailwatch.connectors.homepage import HomepageChecker
from tailwatch.connectors.http import Call, Http, HttpError, Response, fixture_transport

URL = "https://ada.dev"


def page(body: str, title: str = "Ada Lovelace") -> Response:
    markup = (f"<html><head><title>{title}</title><style>p {{color: red}}</style></head><body>"
              "<nav><a href='/'>Home</a> <a href='/blog'>Blog</a></nav>"
              f"<main>{body}</main><script>var x = 1;</script>"
              "<footer>© 2026 Ada Lovelace. Last updated: March 3, 2026.</footer></body></html>")
    return Response(200, markup, {"Content-Type": "text/html; charset=utf-8"}, url=URL)


BODY = ("<h1>Ada Lovelace</h1><p>I am a research scientist at Analytical Engines Ltd. "
        "I work on programmable machines and their notation.</p>"
        "<p>Before that I studied mathematics with Mary Somerville in London for many years.</p>"
        "<p>Find me on <a href='https://github.com/ada'>GitHub</a> and "
        "<a href='https://x.com/ada'>X</a>.</p>")


def ctx(answer: Any, **options: Any) -> CheckContext:
    return CheckContext(http=Http(transport=fixture_transport({"https://": answer})), options=options)


def test_first_read_extracts_readable_text() -> None:
    read = HomepageChecker().read(URL, None, ctx(page(BODY)))
    assert read.status == "ok"
    text = read.content["text"]
    assert "research scientist at Analytical Engines Ltd." in text
    assert "var x" not in text and "color: red" not in text and "Home" not in text  # script, style, nav dropped
    assert "©" not in text  # the footer is dropped
    assert read.content["title"] == "Ada Lovelace"
    assert read.content["links"] == ["github:ada", "x:ada"]


def test_unchanged_page_gives_no_changes() -> None:
    checker = HomepageChecker()
    before = checker.read(URL, None, ctx(page(BODY))).content
    after = checker.read(URL, before, ctx(page(BODY.replace("<p>", "<p>  ")))).content
    assert checker.diff(before, after) == []


def test_a_new_sentence_is_text_changed_with_only_the_removed_and_added_sentences() -> None:
    checker = HomepageChecker()
    before = checker.read(URL, None, ctx(page(BODY))).content
    moved = BODY.replace("research scientist at Analytical Engines Ltd.", "research scientist at Babbage Labs.")
    after = checker.read(URL, before, ctx(page(moved))).content
    changes = checker.diff(before, after)
    assert [c.label for c in changes] == ["text changed"]
    assert changes[0].before == "I am a research scientist at Analytical Engines Ltd."
    assert changes[0].after == "I am a research scientist at Babbage Labs."


def test_reordered_paragraphs_are_not_a_change() -> None:
    checker = HomepageChecker()
    first, second = ("<p>I am a research scientist at Analytical Engines Ltd today.</p>",
                     "<p>Before that I studied mathematics with Mary Somerville in London.</p>")
    before = checker.read(URL, None, ctx(page(first + second))).content
    after = checker.read(URL, before, ctx(page(second + first))).content
    assert checker.diff(before, after) == []


def test_masked_volatile_text_and_title_and_links() -> None:
    checker = HomepageChecker()
    before = checker.read(URL, None, ctx(page(BODY + "<p>Visitors: 1234</p>"))).content
    after = checker.read(URL, before, ctx(page(BODY.replace("https://x.com/ada", "https://x.com/ada_l")
                                                + "<p>Visitors: 1301</p>", title="Ada L."))).content
    labels = [c.label for c in checker.diff(before, after)]
    assert labels == ["title changed", "profile links changed"]


def test_404_and_410_are_gone_and_403_5xx_timeout_are_errors() -> None:
    assert HomepageChecker().read(URL, None, ctx((404, "missing"))).status == "gone"
    assert HomepageChecker().read(URL, None, ctx((410, "gone"))).status == "gone"
    assert HomepageChecker().read(URL, None, ctx((403, "denied"))).status == "error"
    assert HomepageChecker().read(URL, None, ctx((503, "down"))).status == "error"

    def timeout(call: Call) -> Any:
        raise HttpError("timeout reading https://ada.dev")
    assert HomepageChecker().read(URL, None, ctx(timeout)).status == "error"


def test_bot_walls_and_parked_domains_are_errors() -> None:
    wall = Response(200, "<html><title>Just a moment...</title><body>Checking your browser before accessing.</body></html>",
                    {"Content-Type": "text/html"})
    assert HomepageChecker().read(URL, None, ctx(wall)).status == "error"
    captcha = page("<p>Please complete the captcha to continue to this website today.</p>")
    assert HomepageChecker().read(URL, None, ctx(captcha)).status == "error"
    parked = page("<p>This domain is for sale! Buy this domain today.</p>")
    read = HomepageChecker().read(URL, None, ctx(parked))
    assert read.status == "error" and "parking" in read.error


def test_not_html_is_an_error() -> None:
    pdf = Response(200, "%PDF-1.4", {"Content-Type": "application/pdf"})
    assert HomepageChecker().read(URL, None, ctx(pdf)).status == "error"


def test_a_page_shrunk_to_an_empty_shell_is_guarded() -> None:
    checker = HomepageChecker()
    before = checker.read(URL, None, ctx(page(BODY))).content
    shell = checker.read(URL, before, ctx(page("<div id='root'>Loading</div>"))).content
    assert checker.guard(before, shell)


def test_a_short_page_with_a_real_sentence_is_a_change_not_a_failure() -> None:
    checker = HomepageChecker()
    before = checker.read(URL, None, ctx(page(BODY))).content
    moved = checker.read(URL, before, ctx(page("<p>I have moved to babbage.dev, see you there.</p>"))).content
    assert checker.guard(before, moved) == ""
    changes = checker.diff(before, moved)
    assert changes and "I have moved to babbage.dev" in changes[0].after
    # the full page coming back after the short one does not read as all new
    back = checker.read(URL, moved, ctx(page(BODY))).content
    restored = checker.diff(moved, back)
    assert restored and restored[0].after == ""


def test_jina_reader_option() -> None:
    seen: list[str] = []

    def reader(call: Call) -> Response:
        seen.append(call.url)
        return Response(200, "Title: Ada Lovelace\nURL Source: https://ada.dev\nMarkdown Content:\n"
                        "# Ada\n\nI am a research scientist at [Analytical Engines](https://ae.example) in London.\n",
                        url=call.url)
    read = HomepageChecker().read(URL, None, ctx(reader, reader="jina"))
    assert seen == ["https://r.jina.ai/https://ada.dev"]
    assert read.status == "ok" and "research scientist at Analytical Engines in London." in read.content["text"]
    assert read.content["title"] == "Ada Lovelace"
