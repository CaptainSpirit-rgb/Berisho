from scenario import JPM_8K, NOW, NVDA_8K, WATCH, build

from market_intel import config
from market_intel.collectors.filing_text import filing_text_lookup
from market_intel.delivery import analysis, digest, render


def make(con):
    build(con)
    settings = config.load_settings()
    return digest.build_digest(con, WATCH, settings, now=NOW), settings


def test_rule_based_picks_nvda_and_jpm_and_explains(con):
    d, settings = make(con)
    a = analysis.rule_based(d, settings, note="no key")
    assert [i.symbol for i in a.items] == ["NVDA", "JPM"]
    nvda = a.items[0]
    assert "-14.0% on the day" in nvda.headline and "2.4x normal volume" in nvda.headline
    assert "results of operations" in nvda.what
    assert "earnings filing" in nvda.why
    assert nvda.sources == [NVDA_8K]
    jpm = a.items[1]
    assert "Leadership" in jpm.why and jpm.sources == [JPM_8K]
    assert a.summary.startswith("The market is in a BEAR regime: SPY closed")
    assert any(b.startswith("AAPL: -3.8% over 5 days") for b in a.background)
    assert any("Insider selling" in b and "AAPL $35.1M (2 people)" in b for b in a.background)


def test_market_relative_wording(con):
    d, settings = make(con)
    a = analysis.rule_based(d, settings)
    aapl = next(b for b in a.background if b.startswith("AAPL"))
    assert "roughly in line with the market" in aapl  # -3.8% vs SPY about -3%


def test_quiet_day(con):
    d = digest.build_digest(con, WATCH, config.load_settings(), now=NOW)
    a = analysis.rule_based(d, config.load_settings())
    assert a.items == [] and "Nothing on the watchlist stands out" in a.summary
    assert "regime is unknown" in a.summary
    text = render.render_text(d, a)
    assert "Nothing stands out today." in text and "no data yet" in text


def test_render_text_layout(con):
    d, settings = make(con)
    text = render.render_text(d, analysis.rule_based(d, settings, note="no key"))
    order = ["TODAY IN BRIEF", "WHAT MATTERS", "BACKGROUND", "NUMBERS", "FILINGS", "INSIDER"]
    positions = [text.index(s) for s in order]
    assert positions == sorted(positions)
    assert "Fri 20 Nov 2026, 17:00 Asia/Tbilisi" in text
    assert "1. NVDA  125.50 · -14.0% today · -23.0% 5d · vol 2.4x" in text
    assert "18 Nov 01:20 NVDA 8-K 2.02 results of operations" in text  # Tbilisi time
    assert "Claude not used: no key." in " ".join(text.split()) and "not financial advice" in text
    table = text[text.index("NUMBERS") :].splitlines()[1:6]
    assert max(len(line) for line in table) <= 40  # fits a phone
    assert table[-1].startswith("SPY")


def test_render_html_for_telegram(con):
    d, settings = make(con)
    a = analysis.rule_based(d, settings)
    a.items[0].what = "Revenue <fell> & guidance cut"  # must be escaped
    out = render.render_html(d, a)
    assert out.startswith("<b>Market digest</b> · Fri 20 Nov, 17:00 Tbilisi")
    assert "Revenue &lt;fell&gt; &amp; guidance cut" in out
    assert f'<a href="{NVDA_8K}">filing 1</a>' in out
    assert "<pre>" in out and out.count("<pre>") == out.count("</pre>") == 1


def test_rule_based_quotes_the_press_release(con):
    d, settings = make(con)
    a = analysis.rule_based(d, settings, filing_text=filing_text_lookup(con))
    nvda = a.items[0]
    assert 'From the filing: "NVIDIA reports revenue of $10.0 billion."' in nvda.what
    assert '"Outlook: revenue expected to decline."' in nvda.what
    jpm = a.items[1]
    assert "From the filing" not in jpm.what  # no text fetched for it


def test_highlights_pick_item_keywords_verbatim():
    text = (
        "Forward-looking statements are subject to risks and uncertainties of many kinds.\n"
        "On November 17, 2026, Jane Roe informed the Board that she will retire as CFO. "
        "The Board appointed John Doe as Chief Financial Officer, effective January 1, 2027.\n"
        "Short line."
    )
    assert analysis.highlights(text, "5.02,9.01") == [
        "On November 17, 2026, Jane Roe informed the Board that she will retire as CFO.",
        "The Board appointed John Doe as Chief Financial Officer, effective January 1, 2027.",
    ]
    assert analysis.highlights(text, "9.01") == []
    assert analysis.highlights("", "2.02") == []


def test_highlights_skip_cover_page_boilerplate_and_fix_entities():
    text = (
        "JPMorgan Chase &amp;amp; Co. announced Bregje de Best will succeed Jeremy Barnum as CFO.\n"
        "If an emerging growth company, indicate by check mark if the registrant has elected "
        "not to use the extended transition period.\n"
        "• Revenue of $115.8 billion, up 103% from a year ago"
    )
    got = analysis.highlights(text, "5.02,2.02")
    assert got == [
        "JPMorgan Chase & Co. announced Bregje de Best will succeed Jeremy Barnum as CFO.",
        "Revenue of $115.8 billion, up 103% from a year ago",
    ]


def test_check_next_is_specific_to_the_filing(con):
    d, settings = make(con)
    a = analysis.rule_based(d, settings)
    assert "outlook/guidance" in a.items[0].check_next  # NVDA results
    assert "planned succession" in a.items[1].check_next  # JPM officer change
