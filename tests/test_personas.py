import unittest

from psychograph.personas import (
    CHESS,
    COMPACT_PROMPT_LIMIT,
    CUSTOM_REACTION,
    PersonaRegistry,
    can_manage,
    compact_text,
)
from psychograph.settings import Settings
from psychograph.store import Store


class PersonaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.registry = PersonaRegistry(self.store, Settings().personas_dir, "mochi")

    def tearDown(self) -> None:
        self.store.close()

    def test_builtins_load_structured_and_plain_files(self) -> None:
        charlie = self.registry.get(None, "charlie")
        mochi = self.registry.get(None, "mochi")

        self.assertIn("Charlie", charlie.prompt)
        self.assertIn("[Facts]", charlie.prompt)
        self.assertIn("Mochi", mochi.prompt)
        self.assertIn("A Quigley", self.registry.builtin_keys())

    def test_compact_prompts_keep_identity_and_voice(self) -> None:
        mochi, charlie = self.registry.get(None, "mochi"), self.registry.get(None, "charlie")

        self.assertTrue(mochi.compact_prompt.startswith("You are Mochi"))
        self.assertNotIn("## Compact", mochi.prompt)
        self.assertIn("Voice:", charlie.compact_prompt)
        self.assertTrue(charlie.compact_prompt.startswith("You are Charlie"))
        for key in self.registry.builtin_keys():
            self.assertLessEqual(len(self.registry.get(None, key).compact_prompt), COMPACT_PROMPT_LIMIT, key)

    def test_compact_text_falls_back_to_sentences(self) -> None:
        text = "One sentence here. " * 100

        compacted = compact_text(text, limit=100)

        self.assertLessEqual(len(compacted), 100)
        self.assertTrue(compacted.endswith("."))

    def test_personas_get_a_stable_avatar(self) -> None:
        self.assertIn("seed=A%20Quigley", self.registry.get(None, "A Quigley").avatar_url)
        self.assertEqual(self.registry.get(None, "zack").avatar_url, self.registry.get(None, "zack").avatar_url)

    def test_builtin_names_read_naturally_and_stay_reserved(self) -> None:
        persona = self.registry.get(None, "normal_dude")

        self.assertEqual((persona.key, persona.name), ("normal_dude", "normal dude"))
        self.assertEqual(self.registry.find(None, "Normal Dude").key, "normal_dude")
        self.assertTrue(self.registry.is_reserved("normal dude"))
        self.assertTrue(self.registry.is_reserved("normal_dude"))

    def test_uploaded_avatars_apply_per_server_everywhere(self) -> None:
        persona_id = self.store.create_custom_persona(10, 100, "Campfire", "Speak gently.")
        key = f"custom:{persona_id}"
        generated = self.registry.get(10, key).avatar_url
        self.store.set_persona_avatar(10, key, "https://cdn.example/campfire.png", 555, 100)
        self.store.set_persona_avatar(10, "charlie", "https://cdn.example/charlie.png", 556, 1)
        self.store.update_channel(1, persona="charlie")

        self.assertEqual(self.registry.get(10, key).avatar_url, "https://cdn.example/campfire.png")
        self.assertEqual(self.registry.for_channel(1, 10).avatar_url, "https://cdn.example/charlie.png")
        self.assertEqual(self.registry.get(11, "charlie").avatar_url, self.registry.get(None, "charlie").avatar_url)
        by_key = {persona.key: persona.avatar_url for persona in self.registry.available(10)}
        self.assertEqual(by_key["charlie"], "https://cdn.example/charlie.png")
        self.assertNotEqual(generated, by_key[key])

        self.registry.delete(self.registry.get(10, key))
        self.assertIsNone(self.store.persona_avatar(10, key))

    def test_reactions_come_from_persona_files_with_defaults(self) -> None:
        self.assertEqual(self.registry.get(None, "mochi").reaction, "✨")
        self.assertEqual(self.registry.get(None, "normal_dude").reaction, "👋")
        self.assertEqual(self.registry.get(None, "zack").reaction, "✨")

    def test_custom_persona_key_resolves_only_in_its_guild(self) -> None:
        persona_id = self.store.create_custom_persona(10, 100, "Campfire", "Speak gently.")
        key = f"custom:{persona_id}"

        persona = self.registry.get(10, key)
        self.assertEqual((persona.name, persona.prompt, persona.reaction), ("Campfire", "Speak gently.", CUSTOM_REACTION))
        self.assertIsNone(self.registry.get(11, key))
        self.assertIsNone(self.registry.get(None, key))
        self.assertIsNone(self.registry.get(10, "custom:nope"))

    def test_channel_falls_back_to_default_when_its_persona_is_unavailable(self) -> None:
        persona_id = self.store.create_custom_persona(10, 100, "Campfire", "Speak gently.")
        self.store.update_channel(1, persona=f"custom:{persona_id}")

        self.assertEqual(self.registry.for_channel(1, 10).name, "Campfire")
        self.assertEqual(self.registry.for_channel(1, 11).key, "mochi")
        self.assertEqual(self.registry.for_channel(2, 10).key, "mochi")

    def test_find_accepts_typed_names_case_insensitively(self) -> None:
        persona_id = self.store.create_custom_persona(10, 100, "Campfire", "Speak gently.")

        self.assertEqual(self.registry.find(10, "campfire").key, f"custom:{persona_id}")
        self.assertEqual(self.registry.find(10, "chess"), CHESS)
        self.assertIsNone(self.registry.find(10, "nobody"))

    def test_unknown_or_path_like_keys_are_rejected(self) -> None:
        self.assertIsNone(self.registry.get(None, "../config"))
        self.assertIsNone(self.registry.get(None, "missing"))

    def test_available_lists_builtins_chess_and_this_guilds_customs(self) -> None:
        self.store.create_custom_persona(10, 100, "Campfire", "Speak gently.")
        self.store.create_custom_persona(11, 100, "Elsewhere", "Other server.")

        personas = self.registry.available(10)
        names = [persona.name for persona in personas]

        self.assertEqual([p.key for p in personas][: len(self.registry.builtin_keys())], self.registry.builtin_keys())
        self.assertIn("chess", names)
        self.assertIn("Campfire", names)
        self.assertNotIn("Elsewhere", names)
        self.assertEqual([persona.name for persona in self.registry.search(10, "camp", custom_only=True)], ["Campfire"])

    def test_management_is_limited_to_creator_or_server_managers(self) -> None:
        persona = self.registry.get(10, f"custom:{self.store.create_custom_persona(10, 100, 'Campfire', 'Hi.')}")

        self.assertTrue(can_manage(persona, 100, manage_guild=False))
        self.assertFalse(can_manage(persona, 101, manage_guild=False))
        self.assertTrue(can_manage(persona, 101, manage_guild=True))
        self.assertFalse(can_manage(self.registry.get(None, "mochi"), 101, manage_guild=True))

    def test_reserved_names_cover_builtins_and_chess(self) -> None:
        self.assertTrue(self.registry.is_reserved("Charlie"))
        self.assertTrue(self.registry.is_reserved("CHESS"))
        self.assertFalse(self.registry.is_reserved("Campfire"))

    def test_delete_returns_channels_to_default(self) -> None:
        persona = self.registry.get(10, f"custom:{self.store.create_custom_persona(10, 100, 'Campfire', 'Hi.')}")
        self.store.update_channel(1, persona=persona.key)

        self.assertTrue(self.registry.delete(persona))

        self.assertEqual(self.registry.for_channel(1, 10).key, "mochi")
        self.assertFalse(self.registry.delete(self.registry.get(None, "mochi")))


if __name__ == "__main__":
    unittest.main()
