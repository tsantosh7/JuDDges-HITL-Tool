import json
import unittest

from app.propagation import (
    PropagationExample,
    PropagationTarget,
    build_propagated_suggestion_payload,
    clean_openai_values,
    extract_values_with_openai,
    openai_output_text,
)


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class PropagationTests(unittest.TestCase):
    def test_openai_output_text_prefers_top_level_output_text(self):
        self.assertEqual(openai_output_text({"output_text": '{"values": []}'}), '{"values": []}')

    def test_openai_output_text_reads_nested_response_content(self):
        data = {
            "output": [
                {
                    "content": [
                        {"type": "output_text", "text": '{"values": ["guilty plea"]}'},
                    ]
                }
            ]
        }
        self.assertEqual(openai_output_text(data), '{"values": ["guilty plea"]}')

    def test_clean_openai_values_keeps_only_values_found_in_document(self):
        values = ["guilty plea", "not in document", "data not available", "guilty plea"]
        cleaned = clean_openai_values(values, "The appellant entered a guilty plea.", max_doc_chars=1000)
        self.assertEqual(cleaned, ["guilty plea"])

    def test_build_payload_marks_propagated_model_suggestion(self):
        target = PropagationTarget("doc-1", "https://example.test/doc-1", "The offender was remanded.")
        suggestion_id, payload = build_propagated_suggestion_payload(
            group_id="group-1",
            project_id="project-1",
            target=target,
            code="Remand status",
            value="remanded",
            selector=None,
            example_count=2,
            model="gpt-test",
        )

        self.assertTrue(suggestion_id)
        self.assertEqual(payload["group"], "group-1")
        self.assertEqual(payload["tags"][0], "field:Remand status")
        self.assertIn("source:model_suggestion", payload["tags"])
        self.assertIn("propagation:hypothesis_review_examples", payload["tags"])
        self.assertIn("field:Remand status", payload["tags"])
        self.assertFalse(any(tag.startswith("project_id:") for tag in payload["tags"]))
        self.assertFalse(any(tag.startswith("doc_id:") for tag in payload["tags"]))
        self.assertFalse(any(tag.startswith("suggestion_id:") for tag in payload["tags"]))
        self.assertNotIn("target", payload)

    def test_extract_values_with_openai_uses_schema_and_filters_output(self):
        calls = []

        def fake_post(url, headers, json, timeout):
            calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
            return FakeResponse({"output_text": '{"values": ["custody", "hallucinated"]}'})

        values = extract_values_with_openai(
            api_key="test-key",
            model="gpt-test",
            code="Custody",
            examples=[
                PropagationExample(
                    annotation_id="ann-1",
                    document_id="doc-1",
                    code="Custody",
                    value="custody",
                    exact="custody",
                )
            ],
            target=PropagationTarget("doc-2", "https://example.test/doc-2", "The defendant was kept in custody."),
            max_doc_chars=1000,
            timeout=10,
            post=fake_post,
        )

        self.assertEqual(values, ["custody"])
        self.assertEqual(calls[0]["url"], "https://api.openai.com/v1/responses")
        self.assertEqual(calls[0]["json"]["model"], "gpt-test")
        self.assertEqual(calls[0]["json"]["text"]["format"]["type"], "json_schema")


if __name__ == "__main__":
    unittest.main()
