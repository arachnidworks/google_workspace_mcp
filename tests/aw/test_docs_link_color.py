"""AW link_color option for populate_from_markdown (live since df27f781)."""

from gdocs.docs_markdown_writer import markdown_to_docs_requests


def test_link_color_sets_foreground_on_link_range():
    requests = markdown_to_docs_requests(
        "see [AW](https://arachnidworks.com) now", link_color="#8C2433"
    )
    link_styles = [
        r["updateTextStyle"]
        for r in requests
        if "updateTextStyle" in r and "link" in r["updateTextStyle"]["textStyle"]
    ]
    assert len(link_styles) == 1
    style = link_styles[0]
    rgb = style["textStyle"]["foregroundColor"]["color"]["rgbColor"]
    assert abs(rgb["red"] - 0x8C / 255) < 1e-6
    assert abs(rgb["green"] - 0x24 / 255) < 1e-6
    assert abs(rgb["blue"] - 0x33 / 255) < 1e-6
    assert set(style["fields"].split(",")) == {"link", "foregroundColor"}


def test_link_color_none_is_unchanged():
    md = "see [AW](https://arachnidworks.com) now"
    assert markdown_to_docs_requests(md) == markdown_to_docs_requests(
        md, link_color=None
    )


def test_link_color_without_links_adds_nothing():
    md = "# Heading\n\nNo links here."
    assert markdown_to_docs_requests(md, link_color="#8C2433") == (
        markdown_to_docs_requests(md)
    )
