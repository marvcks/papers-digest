"""Regression: .venv/bin/python -m unittest test_news_digest -v."""

from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

import news_digest as nd
import papers_server as server


SOURCE = dict(id="test", name="测试源", url="https://example.com/feed", category="国际综合", language="en")


def article(**fields):
    item = dict(id="one", url="https://example.com/one", source_id="test", source="测试源",
                category="国际综合", language="en", title="A story", title_cn="一则新闻",
                summary="A summary.", summary_cn="一段摘要。", summary_kind="summary",
                published="2026-10-27T01:00:00+00:00", first_seen_at="2026-10-26T01:00:00+00:00")
    item.update(fields)
    return item


class NewsTests(unittest.TestCase):
    def paths(self, folder):
        stack = ExitStack()
        for name, filename in [("ARCHIVE_FILE", "archive.json"), ("STATUS_FILE", "status.json"), ("PAGE_FILE", "news-digest.html")]:
            stack.enter_context(patch.object(nd, name, Path(folder) / filename))
        return stack

    def test_atom_dates_media_caption_and_safe_link(self):
        raw = b'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
          <entry><title>A story</title><link rel="self" href="https://example.com/api"/>
          <link href="https://example.com/a?utm_source=rss&amp;id=3#comments"/>
          <published>2026-10-26T21:00:00-04:00</published>
          <media:description>Image caption</media:description><summary>Real summary</summary>
          <media:content url="https://example.com/a.jpg"/></entry>
          <entry><title>Unsafe</title><link href="javascript:alert(1)"/></entry></feed>'''
        rows = nd.parse_feed(raw, SOURCE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://example.com/a?id=3")
        self.assertEqual(rows[0]["summary"], "Real summary")
        self.assertEqual(rows[0]["published"], "2026-10-27T01:00:00+00:00")

    def test_body_excerpt_has_explicit_kind_and_is_bounded(self):
        raw = ('<rss><channel><item><title>新闻</title><link>https://example.com/one</link>'
               '<description>' + '这是正文。' * 200 + '</description></item></channel></rss>').encode()
        result = nd.parse_feed(raw, dict(SOURCE, language="zh", body_excerpt=True))[0]
        self.assertEqual(result["summary_kind"], "excerpt")
        self.assertLessEqual(len(result["summary"]), 261)
        self.assertEqual(result["summary"], result["summary_cn"])

    def test_transient_tls_failure_retries_feed_without_losing_source(self):
        response = Mock()
        response.content = b'<rss><channel><item><title>News</title><link>https://example.com/one</link><description>Summary</description></item></channel></rss>'
        with patch.object(nd.requests, "get", side_effect=[nd.requests.exceptions.SSLError("temporary handshake failure"), response]) as get:
            rows = nd.fetch_source(SOURCE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(get.call_count, 2)

    def test_missing_description_is_not_labelled_as_body_excerpt(self):
        raw = b'<rss><channel><item><title>News</title><link>https://example.com/one</link></item></channel></rss>'
        item = nd.parse_feed(raw, SOURCE)[0]
        self.assertEqual(item["summary"], "")
        self.assertEqual(item["summary_kind"], "summary")

    def test_newsletter_and_live_blog_promotions_do_not_replace_summary(self):
        text = "Follow the day’s news live Get our breaking news email , free app or daily news podcast Political debate should be respectful. Continue reading..."
        self.assertEqual(nd.clean_summary(text), "Political debate should be respectful.")
        self.assertEqual(nd.clean_summary("This story originally appeared in The Algorithm. To get it first, sign up here. Actual news."), "Actual news.")

    def test_merge_preserves_history_and_invalidates_only_changed_original(self):
        old = article()
        fresh = article(title="Corrected story", title_cn="", summary_cn="", first_seen_at="wrong")
        merged, added = nd.merge_articles([old, article(id="other")], [fresh], "2026-10-27T02:00:00Z")
        saved = next(row for row in merged if row["id"] == "one")
        self.assertEqual(len(merged), 2)
        self.assertEqual(added, 0)
        self.assertEqual(saved["title_cn"], "")
        self.assertEqual(saved["summary_cn"], old["summary_cn"])
        self.assertEqual(saved["first_seen_at"], old["first_seen_at"])

    def test_translation_uses_shared_lock_and_rejects_duplicate_number(self):
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": json.dumps([
            {"id": 1, "title_cn": "新闻一", "summary_cn": "摘要一"},
            {"id": 2, "title_cn": "新闻二", "summary_cn": "摘要二"},
            {"id": 2, "title_cn": "重复", "summary_cn": "重复"},
        ])}}]}
        gate = threading.Lock()
        def request(*args, **kwargs):
            self.assertTrue(gate.locked())
            return response
        rows = [article(title_cn="", summary_cn=""), article(id="two", title_cn="", summary_cn="")]
        with patch.object(nd.requests, "post", side_effect=request):
            done = nd.call_translation(rows, dict(base_url="https://example.com", api_key="test", model="test"), gate)
        self.assertEqual(done, {"one"})
        self.assertFalse(gate.locked())
        self.assertEqual(rows[1]["title_cn"], "")

    def test_unchanged_translations_do_not_call_model(self):
        with patch.object(nd, "call_translation") as call:
            self.assertEqual(nd.translate_articles([article()], lambda _: None), 0)
        call.assert_not_called()

    def test_ecnu_echoed_input_field_names_are_validated_and_cached(self):
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": json.dumps([
            {"id": 1, "title": "中文新闻标题", "summary": "中文新闻摘要"},
            {"id": 2, "title": "An untranslated title", "summary": "An untranslated summary"},
        ])}}]}
        rows = [article(title_cn="", summary_cn=""), article(id="two", title_cn="", summary_cn="")]
        with patch.object(nd.requests, "post", return_value=response):
            done = nd.call_translation(rows, dict(base_url="https://example.com", api_key="test", model="test"), None)
        self.assertEqual(done, {"one"})
        self.assertEqual(rows[0]["title_cn"], "中文新闻标题")
        self.assertEqual(rows[1]["title_cn"], "")

    def test_missing_batch_item_falls_back_without_losing_good_translation(self):
        rows = [article(title_cn="", summary_cn=""), article(id="two", title_cn="", summary_cn="")]
        def translate(batch, config, lock):
            batch[0].update(title_cn="译文", summary_cn="译文")
            return {batch[0]["id"]}
        with patch.object(nd.pd, "get_llm_config", return_value={}), patch.object(nd, "call_translation", side_effect=translate) as call:
            self.assertEqual(nd.translate_articles(rows, lambda _: None), 0)
        self.assertEqual(call.call_count, 2)

    def test_all_failed_fetches_keep_good_page_and_history(self):
        with tempfile.TemporaryDirectory() as folder, self.paths(folder):
            nd.ARCHIVE_FILE.write_text("last archive")
            nd.PAGE_FILE.write_text("last page")
            with patch.object(nd, "load_sources", return_value=[SOURCE]), patch.object(nd, "fetch_source", side_effect=TimeoutError):
                status = nd.update_news()
            self.assertEqual(status["state"], "failed")
            self.assertEqual(nd.ARCHIVE_FILE.read_text(), "last archive")
            self.assertEqual(nd.PAGE_FILE.read_text(), "last page")

    def test_partial_failure_keeps_old_source_and_translations(self):
        with tempfile.TemporaryDirectory() as folder, self.paths(folder):
            nd.ARCHIVE_FILE.write_text(json.dumps({"schema": 1, "articles": [article(), article(id="old")]}))
            fresh = article(title_cn="", summary_cn="")
            def fetch(source):
                if source["id"] == "failed":
                    raise TimeoutError
                return [fresh]
            sources = [SOURCE, dict(SOURCE, id="failed", name="失败源")]
            with patch.object(nd, "load_sources", return_value=sources), patch.object(nd, "fetch_source", side_effect=fetch), patch.object(nd, "call_translation") as model:
                status = nd.update_news()
            self.assertEqual(status["state"], "partial")
            self.assertEqual(status["failures"][0]["name"], "失败源")
            self.assertEqual(len(nd.load_archive()["articles"]), 2)
            model.assert_not_called()

    def test_render_escapes_embedded_script_and_does_not_fetch(self):
        with tempfile.TemporaryDirectory() as folder, self.paths(folder):
            archive = {"schema": 1, "articles": [article(title="</script><script>alert(1)</script>")]}
            with patch.object(nd, "fetch_source") as fetch, patch.object(nd, "call_translation") as model:
                nd.render_page(archive, {})
            page = nd.PAGE_FILE.read_text()
            self.assertNotIn('</script><script>alert(1)', page)
            self.assertIn('<\\/script>', page)
            fetch.assert_not_called()
            model.assert_not_called()

    def test_opening_news_and_polling_never_fetches(self):
        with tempfile.TemporaryDirectory() as folder, self.paths(folder), patch.object(server, "ROOT", Path(folder)):
            http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            worker = threading.Thread(target=http.serve_forever, daemon=True)
            worker.start()
            try:
                with patch.object(nd, "fetch_source") as fetch, patch.object(nd, "call_translation") as model:
                    for path in ["/news", "/api/news/update"]:
                        with urlopen(f"http://127.0.0.1:{http.server_port}{path}") as response:
                            self.assertEqual(response.status, 200)
                    fetch.assert_not_called()
                    model.assert_not_called()
            finally:
                http.shutdown()
                http.server_close()
                worker.join()

    def test_repeated_update_uses_single_background_job(self):
        with patch.object(server, "_news_job_state", {"running": False}), patch.object(server.threading, "Thread") as worker:
            first = server.start_news_update()
            second = server.start_news_update()
        self.assertTrue(first["started"])
        self.assertFalse(second["started"])
        self.assertEqual(worker.call_count, 1)


if __name__ == "__main__":
    unittest.main()
