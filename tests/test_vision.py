"""Photo import tests.

The fixture below is built from a real three-page delivery protocol, including
the two things that actually break a naive importer: codes containing "/", and
the same code appearing twice on one sheet.

No network and no API key: the HTTP call is injected, so payload construction,
parsing, sanitising and duplicate handling are all exercised offline.
"""

import base64
import json
import tempfile
import unittest
from pathlib import Path

from backupov2.core import validate_folder_name
from backupov2.vision import (
    DEFAULT_MODEL,
    MAX_INLINE_BYTES,
    ExtractedItem,
    VisionConfig,
    VisionError,
    build_payload,
    extract_names,
    group_folder_for,
    split_group_heading,
    load_api_key,
    parse_response,
    resolve_duplicates,
    sanitize_name,
    save_api_key,
    to_drafts,
)

# Taken from the real sheets, order preserved.
PAGE_ONE = [
    "ABG-DI-8963-GI-19 R0",
    "ABG-DI-8963-GI-18 R0",
    "ABG-DI-8963-GI-18 R1",
    "ABG-DI-8963-GI-18 R1",  # genuinely printed twice
    "ABG-DI-8963-GI-01 a 10",
    "AGI-DI-9678-NO-01",
]
PAGE_TWO = [
    "AGI-10-0451 R1 a AGI-10-0466 R1 (1/3)",  # "/" is illegal on Windows
    "AGI-10-0451 R1 a AGI-10-0466 R1 (2/3)",
    "AGI-DI-8963-GI-19 R1",
    "AGI-DI-8963-GI-19 R1",  # printed twice
    "ABG-10-0451 R1 a ABG-10-0466 R1 (3/3)  e ABG-10-0466 R0",
]


def api_response(groups) -> dict:
    """A generateContent response shaped the way the schema asks for."""
    return {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "parts": [
                        {
                            "text": json.dumps(
                                {
                                    "document_title": "PROTOCOLO DE ENTREGA DE MATERIAL DE MIDIA",
                                    "box": "CAIXA 02 (EG 1841 ao EG 1852)",
                                    "groups": groups,
                                }
                            )
                        }
                    ]
                },
            }
        ]
    }


def group(label, items, location=""):
    return {
        "label": label,
        "location": location,
        "items": [{"text": text, "uncertain": False} for text in items],
    }


SAMPLE = api_response(
    [
        group("EG 1841", PAGE_ONE + PAGE_TWO, "BENGUELA - ABG"),
        group("EG 1852", ["ALU-3-DI-1852-SD-03", "ALU-3-DI-1852-DE-40 a 118"], "LUANDA - ALU-3"),
        group("EG 1885", ["PAC-DI-1885-GM-01"], "CAPANDA - PAC"),
    ]
)


class SanitizeTests(unittest.TestCase):
    def test_ordinary_code_is_untouched(self) -> None:
        name, issues = sanitize_name("ABG-DI-8963-GI-19 R0")
        self.assertEqual(name, "ABG-DI-8963-GI-19 R0")
        self.assertEqual(issues, [])

    def test_slash_becomes_a_dash_and_is_reported(self) -> None:
        """The real blocker: "(1/3)" cannot be a Windows folder name."""
        name, issues = sanitize_name("AGI-10-0451 R1 a AGI-10-0466 R1 (1/3)")
        self.assertEqual(name, "AGI-10-0451 R1 a AGI-10-0466 R1 (1-3)")
        self.assertTrue(any("invalido" in issue for issue in issues))
        validate_folder_name(name)  # must now be acceptable

    def test_every_forbidden_character_is_handled(self) -> None:
        name, _ = sanitize_name('a<b>c:d"e/f\\g|h?i*j')
        validate_folder_name(name)

    def test_collapses_double_spaces(self) -> None:
        name, issues = sanitize_name("ABG-10-0466 R1 (3/3)  e ABG-10-0466 R0")
        self.assertEqual(name, "ABG-10-0466 R1 (3-3) e ABG-10-0466 R0")
        self.assertIn("espacos ajustados", issues)

    def test_reserved_name_gets_a_suffix(self) -> None:
        name, issues = sanitize_name("CON")
        self.assertEqual(name, "CON_")
        validate_folder_name(name)
        self.assertTrue(any("reservado" in issue for issue in issues))

    def test_trailing_dot_is_dropped(self) -> None:
        name, _ = sanitize_name("Disco 01.")
        self.assertEqual(name, "Disco 01")
        validate_folder_name(name)

    def test_empty_line_becomes_a_placeholder(self) -> None:
        name, issues = sanitize_name("   ")
        self.assertEqual(name, "SEM NOME")
        self.assertIn("linha vazia", issues)

    def test_overlong_name_is_shortened(self) -> None:
        name, issues = sanitize_name("A" * 300)
        self.assertLessEqual(len(name), 120)
        self.assertIn("nome encurtado", issues)
        validate_folder_name(name)


class DuplicateTests(unittest.TestCase):
    def items(self, names):
        return [ExtractedItem(raw_text=n, folder_name=n) for n in names]

    def test_repeat_is_suffixed_not_dropped(self) -> None:
        """A repeated code means two physical discs - losing one loses a disc."""
        items = resolve_duplicates(self.items(["A", "B", "A"]))
        self.assertEqual([i.folder_name for i in items], ["A", "B", "A (2)"])
        self.assertEqual(len(items), 3)

    def test_repeat_is_flagged_for_review(self) -> None:
        items = resolve_duplicates(self.items(["A", "A"]))
        self.assertFalse(items[0].needs_review)
        self.assertTrue(items[1].needs_review)

    def test_matching_is_case_insensitive(self) -> None:
        items = resolve_duplicates(self.items(["Disco", "disco"]))
        self.assertEqual(items[1].folder_name, "disco (2)")

    def test_three_of_a_kind(self) -> None:
        items = resolve_duplicates(self.items(["A", "A", "A"]))
        self.assertEqual([i.folder_name for i in items], ["A", "A (2)", "A (3)"])


class PayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.page = self.root / "pagina1.jpg"
        self.page.write_bytes(b"\xff\xd8fake-jpeg")

    def test_prompt_comes_first_then_pages_in_order(self) -> None:
        second = self.root / "pagina2.png"
        second.write_bytes(b"\x89PNGfake")
        payload = build_payload([self.page, second])

        parts = payload["contents"][0]["parts"]
        self.assertIn("PROTOCOLO", parts[0]["text"])
        self.assertEqual(parts[1]["inline_data"]["mime_type"], "image/jpeg")
        self.assertEqual(parts[2]["inline_data"]["mime_type"], "image/png")
        self.assertEqual(
            base64.b64decode(parts[1]["inline_data"]["data"]), b"\xff\xd8fake-jpeg"
        )

    def test_prompt_warns_about_bleed_through(self) -> None:
        """Thin paper shows the back of the sheet; transcribing it invents discs."""
        text = build_payload([self.page])["contents"][0]["parts"][0]["text"]
        self.assertIn("espelhado", text)
        self.assertIn("fantasma", text)

    def test_prompt_forbids_dropping_duplicates(self) -> None:
        text = build_payload([self.page])["contents"][0]["parts"][0]["text"]
        self.assertIn("duplicatas", text)

    def test_asks_for_deterministic_json(self) -> None:
        config = build_payload([self.page])["generationConfig"]
        self.assertEqual(config["temperature"], 0.0)
        self.assertEqual(config["responseMimeType"], "application/json")
        self.assertIn("groups", config["responseSchema"]["properties"])

    def test_pdf_is_accepted(self) -> None:
        pdf = self.root / "digitalizado.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        payload = build_payload([pdf])
        self.assertEqual(
            payload["contents"][0]["parts"][1]["inline_data"]["mime_type"],
            "application/pdf",
        )

    def test_unsupported_format_is_rejected(self) -> None:
        bad = self.root / "planilha.xlsx"
        bad.write_bytes(b"x")
        with self.assertRaises(VisionError):
            build_payload([bad])

    def test_no_images_is_rejected(self) -> None:
        with self.assertRaises(VisionError):
            build_payload([])

    def test_oversized_batch_is_rejected_before_sending(self) -> None:
        big = self.root / "grande.jpg"
        big.write_bytes(b"x" * (MAX_INLINE_BYTES + 1))
        with self.assertRaises(VisionError) as caught:
            build_payload([big])
        self.assertIn("MB", str(caught.exception))


class ParseTests(unittest.TestCase):
    def test_reads_every_code_in_order(self) -> None:
        result = parse_response(SAMPLE)
        self.assertEqual(len(result.items), len(PAGE_ONE) + len(PAGE_TWO) + 3)
        self.assertEqual(result.items[0].raw_text, "ABG-DI-8963-GI-19 R0")
        self.assertEqual(result.items[-1].raw_text, "PAC-DI-1885-GM-01")

    def test_keeps_group_and_location(self) -> None:
        result = parse_response(SAMPLE)
        self.assertEqual(result.items[0].group_label, "EG 1841")
        self.assertEqual(result.items[0].group_location, "BENGUELA - ABG")
        self.assertEqual(result.group_labels(), ["EG 1841", "EG 1852", "EG 1885"])

    def test_box_and_title_are_captured(self) -> None:
        result = parse_response(SAMPLE)
        self.assertIn("CAIXA 02", result.box)

    def test_every_proposed_name_is_a_legal_folder(self) -> None:
        """The whole point: nothing reaches the job that Windows would refuse."""
        for item in parse_response(SAMPLE).items:
            with self.subTest(name=item.folder_name):
                validate_folder_name(item.folder_name)

    def test_names_are_unique_after_parsing(self) -> None:
        names = [i.folder_name.casefold() for i in parse_response(SAMPLE).items]
        self.assertEqual(len(names), len(set(names)))

    def test_problem_lines_are_flagged(self) -> None:
        result = parse_response(SAMPLE)
        flagged = [i for i in result.items if i.needs_review]
        self.assertTrue(flagged)
        self.assertEqual(result.review_count, len(flagged))

    def test_blank_lines_are_skipped(self) -> None:
        result = parse_response(api_response([group("EG 1", ["A", "   ", "B"])]))
        self.assertEqual([i.raw_text for i in result.items], ["A", "B"])

    def test_plain_string_items_are_accepted(self) -> None:
        """Tolerate a model that returns bare strings instead of objects."""
        payload = api_response([{"label": "EG 1", "location": "", "items": ["A", "B"]}])
        self.assertEqual(len(parse_response(payload).items), 2)

    def test_uncertain_flag_is_carried_through(self) -> None:
        payload = api_response(
            [{"label": "EG 1", "items": [{"text": "A?B", "uncertain": True}]}]
        )
        item = parse_response(payload).items[0]
        self.assertTrue(item.uncertain)
        self.assertTrue(item.needs_review)

    def test_truncated_response_warns(self) -> None:
        payload = api_response([group("EG 1", ["A"])])
        payload["candidates"][0]["finishReason"] = "MAX_TOKENS"
        self.assertTrue(parse_response(payload).warnings)

    def test_blocked_prompt_raises(self) -> None:
        with self.assertRaises(VisionError):
            parse_response({"promptFeedback": {"blockReason": "SAFETY"}})

    def test_no_candidates_raises(self) -> None:
        with self.assertRaises(VisionError):
            parse_response({"candidates": []})

    def test_non_json_text_raises(self) -> None:
        payload = {"candidates": [{"content": {"parts": [{"text": "desculpe, nao consegui"}]}}]}
        with self.assertRaises(VisionError):
            parse_response(payload)

    def test_empty_list_raises(self) -> None:
        with self.assertRaises(VisionError):
            parse_response(api_response([group("EG 1", [])]))


class GroupHeadingTests(unittest.TestCase):
    """The model does not reliably split the heading, so the code must.

    Observed in the wild: "label" came back as the whole printed line,
    "- EG 1841 ( BEBGUELA - ABG )", while "location" repeated the same text.
    Appending the location produced folders like
    "- EG 1841 ( BEBGUELA - ABG ) (BEBGUELA - ABG)".
    """

    def test_whole_heading_in_the_label_is_not_doubled(self) -> None:
        key, location = split_group_heading(
            "- EG 1841 ( BEBGUELA - ABG )", "BEBGUELA - ABG"
        )
        self.assertEqual(key, "EG 1841")
        self.assertEqual(location, "BEBGUELA - ABG")
        self.assertEqual(
            group_folder_for(key, location), "EG 1841 (BEBGUELA - ABG)"
        )

    def test_leading_dash_is_stripped(self) -> None:
        for prefix in ("- ", "– ", "— ", ""):
            with self.subTest(prefix=prefix):
                key, _ = split_group_heading(f"{prefix}EG 1841", "")
                self.assertEqual(key, "EG 1841")

    def test_location_taken_from_the_label_when_absent(self) -> None:
        key, location = split_group_heading("- EG 1852 ( LUANDA - ALU-3 )", "")
        self.assertEqual(key, "EG 1852")
        self.assertEqual(location, "LUANDA - ALU-3")

    def test_already_split_heading_is_untouched(self) -> None:
        self.assertEqual(
            split_group_heading("EG 1841", "BENGUELA - ABG"),
            ("EG 1841", "BENGUELA - ABG"),
        )

    def test_key_normalises_spacing(self) -> None:
        self.assertEqual(split_group_heading("EG1852", "")[0], "EG 1852")
        self.assertEqual(split_group_heading("eg  1852", "")[0], "EG 1852")

    def test_dash_separated_heading(self) -> None:
        self.assertEqual(split_group_heading("EG 1841 - BENGUELA", "")[0], "EG 1841")

    def test_non_eg_heading_is_kept_as_written(self) -> None:
        self.assertEqual(split_group_heading("PREG 1841", "")[0], "PREG 1841")

    def test_no_heading_means_no_folder(self) -> None:
        self.assertEqual(group_folder_for("", "qualquer"), "")


class OneFolderPerGroupTests(unittest.TestCase):
    """A sheet spanning pages can describe the same EG twice, differently.

    That produced two folders for one EG, splitting its discs. One location is
    chosen per EG so every disc of that EG lands together.
    """

    def messy(self):
        return api_response(
            [
                {
                    "label": "- EG 1841 ( BEBGUELA - ABG )",
                    "location": "BEBGUELA - ABG",
                    "items": ["A1", "A2"],
                },
                {
                    "label": "- EG 1841 ( BEBGUELA - ABG )",
                    "location": "BEBGUELA",
                    "items": ["A3"],
                },
                {
                    "label": "- EG 1885 ( CAPANDA - PAC )",
                    "location": "CAPANDA",
                    "items": ["P1"],
                },
            ]
        )

    def test_one_folder_per_eg(self) -> None:
        items = parse_response(self.messy()).items
        folders = {item.group_folder_name() for item in items}
        self.assertEqual(len(folders), 2, folders)

    def test_the_most_detailed_location_wins(self) -> None:
        items = parse_response(self.messy()).items
        self.assertEqual(items[2].group_folder_name(), "EG 1841 (BEBGUELA - ABG)")

    def test_all_discs_of_an_eg_share_one_folder(self) -> None:
        items = parse_response(self.messy()).items
        eg1841 = [i.group_folder_name() for i in items if i.folder_name.startswith("A")]
        self.assertEqual(len(set(eg1841)), 1)
        self.assertEqual(len(eg1841), 3)

    def test_no_folder_repeats_the_location(self) -> None:
        for item in parse_response(self.messy()).items:
            with self.subTest(folder=item.group_folder_name()):
                self.assertEqual(item.group_folder_name().count("("), 1)
                self.assertFalse(item.group_folder_name().startswith("-"))

    def test_folders_are_legal_windows_names(self) -> None:
        for item in parse_response(self.messy()).items:
            validate_folder_name(item.group_folder_name())


class GroupFolderTests(unittest.TestCase):
    """The EG becomes a real subfolder, not a prefix on the disc's name."""

    def test_group_folder_combines_label_and_location(self) -> None:
        item = parse_response(SAMPLE).items[0]
        self.assertEqual(item.group_folder_name(), "EG 1841 (BENGUELA - ABG)")
        validate_folder_name(item.group_folder_name())

    def test_group_folder_without_a_location(self) -> None:
        payload = api_response([{"label": "EG 1841", "items": ["A"]}])
        self.assertEqual(parse_response(payload).items[0].group_folder_name(), "EG 1841")

    def test_no_group_means_no_subfolder(self) -> None:
        payload = api_response([{"label": "", "items": ["A"]}])
        self.assertEqual(parse_response(payload).items[0].group_folder_name(), "")

    def test_every_group_folder_is_a_legal_name(self) -> None:
        for item in parse_response(SAMPLE).items:
            with self.subTest(group=item.group_folder_name()):
                if item.group_folder_name():
                    validate_folder_name(item.group_folder_name())

    def test_disc_names_are_not_prefixed(self) -> None:
        """The EG lives in the path, so the leaf name stays as printed."""
        item = parse_response(SAMPLE).items[0]
        self.assertEqual(item.folder_name, "ABG-DI-8963-GI-19 R0")

    def test_drafts_carry_the_group(self) -> None:
        drafts = to_drafts(parse_response(SAMPLE).items)
        self.assertEqual(drafts[0].group, "EG 1841 (BENGUELA - ABG)")
        self.assertEqual(drafts[-1].group, "EG 1885 (CAPANDA - PAC)")

    def test_grouping_can_be_turned_off(self) -> None:
        drafts = to_drafts(parse_response(SAMPLE).items, use_groups=False)
        self.assertTrue(all(draft.group == "" for draft in drafts))

    def test_repeat_in_the_same_group_is_still_suffixed(self) -> None:
        """Same folder, so it would collide."""
        payload = api_response([group("EG 1841", ["A", "A"], "BENGUELA - ABG")])
        names = [i.folder_name for i in parse_response(payload).items]
        self.assertEqual(names, ["A", "A (2)"])

    def test_same_code_in_two_groups_is_left_alone(self) -> None:
        """Different folders, so there is nothing to disambiguate."""
        payload = api_response(
            [group("EG 1841", ["A"], "BENGUELA - ABG"), group("EG 1852", ["A"], "LUANDA")]
        )
        items = parse_response(payload).items
        self.assertEqual([i.folder_name for i in items], ["A", "A"])
        self.assertNotEqual(items[0].group_folder_name(), items[1].group_folder_name())


class DraftTests(unittest.TestCase):
    def test_drafts_carry_provenance(self) -> None:
        result = parse_response(SAMPLE)
        drafts = to_drafts(result.items, model=DEFAULT_MODEL)

        self.assertEqual(len(drafts), len(result.items))
        self.assertEqual(drafts[0].source, "llm_photo")
        self.assertEqual(drafts[0].llm["model"], DEFAULT_MODEL)
        self.assertEqual(drafts[0].llm["raw_text"], "ABG-DI-8963-GI-19 R0")
        self.assertEqual(drafts[0].llm["group"], "EG 1841")

    def test_raw_text_is_kept_when_the_name_was_changed(self) -> None:
        """So the user can always see what the paper actually said."""
        result = parse_response(SAMPLE)
        changed = [i for i in result.items if i.was_changed]
        self.assertTrue(changed)

        # Names change for two different reasons, and both keep the original.
        with_slash = [i for i in changed if "/" in i.raw_text]
        self.assertTrue(with_slash, "esperava linhas com barra")
        for item in with_slash:
            self.assertNotIn("/", item.folder_name)

        suffixed = [i for i in changed if i.folder_name.endswith("(2)")]
        self.assertTrue(suffixed, "esperava uma linha repetida")
        self.assertFalse(suffixed[0].raw_text.endswith("(2)"))

    def test_uncertain_items_get_lower_confidence(self) -> None:
        payload = api_response(
            [{"label": "EG 1", "items": [{"text": "A", "uncertain": True}]}]
        )
        draft = to_drafts(parse_response(payload).items)[0]
        self.assertLess(draft.name_confidence, 0.9)
        self.assertTrue(draft.needs_review)


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_endpoint_includes_the_model(self) -> None:
        config = VisionConfig(api_key="k", model="models/gemini-3.8-flash")
        self.assertTrue(config.endpoint.endswith("models/gemini-3.8-flash:generateContent"))

    def test_bare_model_name_is_prefixed(self) -> None:
        config = VisionConfig(api_key="k", model="gemini-3.8-flash")
        self.assertIn("/models/gemini-3.8-flash:", config.endpoint)

    def test_key_roundtrips_through_the_config_file(self) -> None:
        save_api_key("segredo-123", local_root=self.root)
        self.assertEqual(load_api_key(local_root=self.root), "segredo-123")

    def test_environment_wins_over_the_file(self) -> None:
        import os

        save_api_key("do-arquivo", local_root=self.root)
        os.environ["GEMINI_API_KEY"] = "do-ambiente"
        try:
            self.assertEqual(load_api_key(local_root=self.root), "do-ambiente")
        finally:
            del os.environ["GEMINI_API_KEY"]

    def test_missing_config_is_empty_not_an_error(self) -> None:
        self.assertEqual(load_api_key(local_root=self.root / "nada"), "")


class ExtractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.page = self.root / "p1.jpg"
        self.page.write_bytes(b"\xff\xd8fake")

    def test_end_to_end_with_an_injected_transport(self) -> None:
        sent = {}

        def fake_post(url, body, key, timeout):
            sent.update(url=url, body=body, key=key)
            return SAMPLE

        config = VisionConfig(api_key="chave", model=DEFAULT_MODEL)
        result = extract_names([self.page], config, post=fake_post)

        self.assertEqual(result.model, DEFAULT_MODEL)
        self.assertGreater(len(result.items), 10)
        self.assertEqual(sent["key"], "chave")
        self.assertIn("generateContent", sent["url"])

    def test_missing_key_fails_before_any_request(self) -> None:
        def explode(*args, **kwargs):
            raise AssertionError("nao deveria chamar a API")

        with self.assertRaises(VisionError) as caught:
            extract_names([self.page], VisionConfig(api_key=""), post=explode)
        self.assertIn("chave", str(caught.exception).lower())

    def test_the_key_is_not_placed_in_the_url(self) -> None:
        """It travels as a header so it never lands in a log or a proxy trace."""
        captured = {}

        def fake_post(url, body, key, timeout):
            captured["url"] = url
            return SAMPLE

        extract_names([self.page], VisionConfig(api_key="super-secreta"), post=fake_post)
        self.assertNotIn("super-secreta", captured["url"])


if __name__ == "__main__":
    unittest.main()
